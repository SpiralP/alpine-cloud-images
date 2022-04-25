# NOTE: not meant to be executed directly
# vim: ts=4 et:

import logging
import hashlib
import os
import time

from datetime import datetime
from subprocess import Popen, PIPE, run

from .interfaces.adapter import CloudAdapterInterface
from image_configs import Tags, DictObj


class AWSCloudAdapter(CloudAdapterInterface):
    IMAGE_INFO = [
        'revision', 'imported', 'import_id', 'import_region', 'published',
    ]
    CRED_MAP = {
        'access_key': 'aws_access_key_id',
        'secret_key': 'aws_secret_access_key',
        'session_token': 'aws_session_token',
    }
    CONVERT_CMD = (
        'qemu-img', 'convert', '-f', 'qcow2', '-O', 'vpc', '-o', 'force_size=on'
    )
    ARCH = {
        'aarch64': 'arm64',
        'x86_64': 'x86_64',
    }
    BOOT_MODE = {
        'bios': 'legacy-bios',
        'uefi': 'uefi',
    }

    @property
    def sdk(self):
        # delayed import/install of SDK until we want to use it
        if not self._sdk:
            try:
                import boto3
            except ModuleNotFoundError:
                run(['work/bin/pip', 'install', '-U', 'boto3'])
                import boto3

            self._sdk = boto3

        return self._sdk

    def session(self, region=None):
        if region not in self._sessions:
            creds = {'region_name': region} | self.credentials(region)
            self._sessions[region] = self.sdk.session.Session(**creds)

        return self._sessions[region]

    @property
    def regions(self):
        if self.cred_provider:
            return self.cred_provider.get_regions(self.cloud)

        # list of all subscribed regions
        return {r['RegionName']: True for r in self.session().client('ec2').describe_regions()['Regions']}

    @property
    def default_region(self):
        if self.cred_provider:
            return self.cred_provider.get_default_region(self.cloud)

        # rely on our env or ~/.aws config for the default
        return None

    def credentials(self, region=None):
        if not self.cred_provider:
            # use the cloud SDK's default credential discovery
            return {}

        creds = self.cred_provider.get_credentials(self.cloud, region)
        # return dict suitable to use for session()
        return {self.CRED_MAP[k]: v for k, v in creds.items() if k in self.CRED_MAP}

    def _get_images_with_tags(self, project, image_key, tags={}, region=None):
        ec2r = self.session(region).resource('ec2')
        req = {'Owners': ['self'], 'Filters': []}
        tags |= {
            'project': project,
            'image_key': image_key,
        }
        for k, v in tags.items():
            req['Filters'].append({'Name': f"tag:{k}", 'Values': [str(v)]})

        return sorted(
            ec2r.images.filter(**req), key=lambda k: k.creation_date, reverse=True)

    # necessary cloud-agnostic image info
    def _image_info(self, i):
        tags = Tags(from_list=i.tags)
        return DictObj({k: tags.get(k, None) for k in self.IMAGE_INFO})

    # get the latest imported image for a given build name
    def latest_build_image(self, project, image_key):
        images = self._get_images_with_tags(
            project=project,
            image_key=image_key,
        )
        if images:
            # first one is the latest
            return self._image_info(images[0])

        return None

    # import an image
    # NOTE: requires 'vmimport' role with read/write of <s3_bucket>.* and its objects
    def import_image(self, ic):
        log = logging.getLogger('import')
        image_path = ic.local_path
        image_aws = ic.local_dir / 'image.vhd'
        name = ic.image_name
        description = ic.image_description

        # convert QCOW2 to VHD
        log.info('Converting %s to VHD format', image_path)
        p = Popen(self.CONVERT_CMD + (image_path, image_aws), stdout=PIPE, stdin=PIPE, encoding='utf8')
        out, err = p.communicate()
        if p.returncode:
            log.error('Unable to convert %s to VHD format (%s)', image_path, p.returncode)
            log.error('EXIT: %d', p.returncode)
            log.error('STDOUT:\n%s', out)
            log.error('STDERR:\n%s', err)
            raise RuntimeError

        session = self.session()
        s3r = session.resource('s3')
        ec2c = session.client('ec2')
        ec2r = session.resource('ec2')

        bucket_name = 'alpine-cloud-images.' + hashlib.sha1(os.urandom(40)).hexdigest()
        s3_key = name + '.vhd'

        bucket = s3r.Bucket(bucket_name)
        log.info('Creating S3 bucket %s', bucket.name)
        bucket.create(
            CreateBucketConfiguration={'LocationConstraint': ec2c.meta.region_name}
        )
        bucket.wait_until_exists()
        s3_url = f"s3://{bucket.name}/{s3_key}"

        try:
            log.info('Uploading %s to %s', image_aws, s3_url)
            bucket.upload_file(str(image_aws), s3_key)

            # import snapshot from S3
            log.info('Importing EC2 snapshot from %s', s3_url)
            ss_import = ec2c.import_snapshot(
                DiskContainer={
                    'Description': description,     # https://github.com/boto/boto3/issues/2286
                    'Format': 'VHD',
                    'Url': s3_url
                }
                # NOTE: TagSpecifications -- doesn't work with ResourceType: snapshot?
            )
            ss_task_id = ss_import['ImportTaskId']
            while True:
                ss_task = ec2c.describe_import_snapshot_tasks(
                    ImportTaskIds=[ss_task_id]
                )
                task_detail = ss_task['ImportSnapshotTasks'][0]['SnapshotTaskDetail']
                if task_detail['Status'] not in ['pending', 'active', 'completed']:
                    msg = f"Bad EC2 snapshot import: {task_detail['Status']} - {task_detail['StatusMessage']}"
                    log.error(msg)
                    raise RuntimeError(msg)

                if task_detail['Status'] == 'completed':
                    snapshot_id = task_detail['SnapshotId']
                    break

                time.sleep(15)
        except Exception:
            log.error('Unable to import snapshot from S3:', exc_info=True)
            raise
        finally:
            # always cleanup S3, even if there was an exception raised
            log.info('Cleaning up %s', s3_url)
            bucket.Object(s3_key).delete()
            bucket.delete()

        # tag snapshot
        snapshot = ec2r.Snapshot(snapshot_id)
        try:
            log.info('Tagging EC2 snapshot %s', snapshot_id)
            tags = ic.tags
            tags.Name = tags.name   # because AWS is special
            snapshot.create_tags(Tags=tags.as_list())
        except Exception:
            log.error('Unable to tag snapshot:', exc_info=True)
            log.info('Removing snapshot')
            snapshot.delete()
            raise

        # register AMI
        try:
            log.info('Registering EC2 AMI from snapshot %s', snapshot_id)
            img = ec2c.register_image(
                Architecture=self.ARCH[ic.arch],
                BlockDeviceMappings=[{
                    'DeviceName': '/dev/xvda',
                    'Ebs': {
                        'SnapshotId': snapshot_id,
                        'DeleteOnTermination': True,
                        'VolumeSize': 1,
                        'VolumeType': 'gp3',
                        'Iops': 3000,
                        'Throughput': 125,
                    }
                }],
                Description=description,
                EnaSupport=True,
                Name=ic.image_name,
                RootDeviceName='/dev/xvda',
                SriovNetSupport='simple',
                VirtualizationType='hvm',
                BootMode=self.BOOT_MODE[ic.firmware],
            )
        except Exception:
            log.error('Unable to register image:', exc_info=True)
            log.info('Removing snapshot')
            snapshot.delete()
            raise

        image_id = img['ImageId']
        image = ec2r.Image(image_id)

        try:
            # tag image (adds imported tag)
            log.info('Tagging EC2 AMI %s', image_id)
            tags.imported = datetime.utcnow().isoformat()
            tags.import_id = image_id
            tags.import_region = ec2c.meta.region_name
            image.create_tags(Tags=tags.as_list())
        except Exception:
            log.error('Unable to tag image:', exc_info=True)
            log.info('Removing image and snapshot')
            image.delete()
            snapshot.delete()
            raise

        return self._image_info(image)

    # remove an (unpublished) image
    def remove_image(self, image_id):
        log = logging.getLogger('build')
        ec2r = self.session().resource('ec2')
        image = ec2r.Image(image_id)
        snapshot_id = image.block_device_mappings[0]['Ebs']['SnapshotId']
        snapshot = ec2r.Snapshot(snapshot_id)
        log.info('Deregistering %s', image_id)
        image.deregister()
        log.info('Deleting %s', snapshot_id)
        snapshot.delete()

    # publish an image
    def publish_image(self, ic):
        log = logging.getLogger('publish')
        source_image = self.latest_build_image(
            ic.project,
            ic.image_key,
        )
        if not source_image:
            log.error('No source image for %s', ic.image_key)
            raise RuntimeError('Missing source imamge')

        source_id = source_image.import_id
        source_region = source_image.import_region
        log.info('Publishing source: %s/%s', source_region, source_id)
        source = self.session().resource('ec2').Image(source_id)

        # we may be updating tags, get them from image config
        tags = ic.tags

        # sort out published image access permissions
        perms = {'groups': [], 'users': []}
        if ic.access.get('PUBLIC', None):
            perms['groups'] = ['all']
        else:
            for k, v in ic.access.items():
                if v:
                    log.debug('users: %s', k)
                    perms['users'].append(str(k))

        log.debug('perms: %s', perms)

        # resolve destination regions
        regions = self.regions
        if ic.regions.pop('ALL', None):
            log.info('Publishing to ALL available regions')
        else:
            # clear ALL out of the way if it's still there
            ic.regions.pop('ALL', None)
            regions = {r: regions[r] for r in ic.regions}

        publishing = {}
        for r in regions.keys():
            if not regions[r]:
                log.warning('Skipping unsubscribed AWS region %s', r)
                continue

            images = self._get_images_with_tags(
                region=r,
                project=ic.project,
                image_key=ic.image_key,
                tags={'revision': ic.revision}
            )
            if images:
                image = images[0]
                log.info('%s: Already exists as %s', r, image.id)
            else:
                ec2c = self.session(r).client('ec2')
                try:
                    res = ec2c.copy_image(
                        Description=source.description,
                        Name=source.name,
                        SourceImageId=source_id,
                        SourceRegion=source_region,
                    )
                except Exception:
                    log.warning('Skipping %s, unable to copy image:', r, exc_info=True)
                    continue

                image_id = res['ImageId']
                log.info('%s: Publishing to %s', r, image_id)
                image = self.session(r).resource('ec2').Image(image_id)

            publishing[r] = image

        artifacts = {}
        copy_wait = 180
        while len(artifacts) < len(publishing):
            for r, image in publishing.items():
                if r not in artifacts:
                    image.reload()
                    if image.state == 'available':
                        # tag image
                        log.info('%s: Adding tags to %s', r, image.id)
                        image_tags = Tags(from_list=image.tags)
                        fresh = False
                        if 'published' not in image_tags:
                            fresh = True

                        if fresh:
                            tags.published = datetime.utcnow().isoformat()

                        image.create_tags(Tags=tags.as_list())

                        # tag image's snapshot, too
                        snapshot = self.session(r).resource('ec2').Snapshot(
                            image.block_device_mappings[0]['Ebs']['SnapshotId']
                        )
                        snapshot.create_tags(Tags=tags.as_list())

                        # update image description to match description in tags
                        log.info('%s: Updating description to %s', r, tags.description)
                        image.modify_attribute(
                            Description={'Value': tags.description},
                        )

                        # apply launch perms
                        log.info('%s: Applying launch perms to %s', r, image.id)
                        image.reset_attribute(Attribute='launchPermission')
                        image.modify_attribute(
                            Attribute='launchPermission',
                            OperationType='add',
                            UserGroups=perms['groups'],
                            UserIds=perms['users'],
                        )

                        # set up AMI deprecation
                        ec2c = image.meta.client
                        log.info('%s: Setting EOL deprecation time on %s', r, image.id)
                        ec2c.enable_image_deprecation(
                            ImageId=image.id,
                            DeprecateAt=f"{tags.end_of_life}T23:59:59Z"
                        )

                        artifacts[r] = image.id

                    if image.state == 'failed':
                        log.error('%s: %s - %s - %s', r, image.id, image.state, image.state_reason)
                        artifacts[r] = None

            remaining = len(publishing) - len(artifacts)
            if remaining > 0:
                log.info('Waiting %ds for %d images to complete', copy_wait, remaining)
                time.sleep(copy_wait)
                copy_wait = 30

        return artifacts


def register(cloud, cred_provider=None):
    return AWSCloudAdapter(cloud, cred_provider)
