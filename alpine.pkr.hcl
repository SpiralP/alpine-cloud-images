# Alpine Cloud Images Packer Configuration

### Variables

# include debug output from provisioning/post-processing scripts
variable "DEBUG" {
  default = 0
}
# indicates cloud_helper.py should be run with --use-broker
variable "USE_BROKER" {
  default = 0
}

# tuneable QEMU VM parameters, based on perfomance of the local machine;
# overrideable via build script --vars parameter referencing a Packer
# ".vars.hcl" file containing alternate settings
variable "qemu" {
  default = {
    boot_wait = {
      aarch64 = "1m"
      x86_64  = "1m"
    }
    cmd_wait    = "5s"
    ssh_timeout = "1m"
    memory      = 1024  # MiB
  }
}

### Local Data

locals {
  debug_arg   = var.DEBUG == 0 ? "" : "--debug"
  broker_arg  = var.USE_BROKER == 0 ? "" : "--use-broker"

  # randomly generated password
  password = uuidv4()

  # resolve actionable build configs
  configs = { for b, cfg in yamldecode(file("work/images.yaml")):
    b => cfg if contains(keys(cfg), "actions")
  }
}

### Build Sources

# Don't build
source null alpine {
  communicator = "none"
}

# Common to all QEMU builds
source qemu alpine {
  # qemu machine
  headless          = true
  memory            = var.qemu.memory
  net_device        = "virtio-net"
  disk_interface    = "virtio"

  # build environment
  boot_command = [
    "root<enter>",
    "setup-interfaces<enter><enter><enter><enter>",
    "ifup eth0<enter><wait${var.qemu.cmd_wait}>",
    "setup-sshd -c openssh<enter><wait${var.qemu.cmd_wait}>",
    "echo PermitRootLogin yes >> /etc/ssh/sshd_config<enter>",
    "service sshd restart<enter>",
    "echo 'root:${local.password}' | chpasswd<enter>",
  ]
  ssh_username      = "root"
  ssh_password      = local.password
  ssh_timeout       = var.qemu.ssh_timeout
  shutdown_command  = "poweroff"
}

build {
  name = "alpine"

  ## Builders

  # QEMU builder
  dynamic "source" {
    for_each = { for b, c in local.configs:
        b => c if contains(c.actions, "build") && c.builder == "qemu"
      }
    iterator = B
    labels = ["qemu.alpine"]  # links us to the base source

    content {
      name = B.key

      # qemu machine
      qemu_binary   = "qemu-system-${B.value.arch}"
      qemuargs      = B.value.qemu.args
      machine_type  = B.value.qemu.machine_type
      firmware      = B.value.qemu.firmware

      # build environment
      iso_url       = B.value.qemu.iso_url
      iso_checksum  = "file:${B.value.qemu.iso_url}.sha512"
      boot_wait     = var.qemu.boot_wait[B.value.arch]

      # results
      output_directory  = "work/images/${B.value.cloud}/${B.value.image_key}"
      disk_size         = B.value.size
      format            = B.value.local_format
      vm_name           = "image.${B.value.local_format}"

      accelerator       = B.value.arch == "aarch64" ? "tcg" : "kvm"
    }
  }

  # Null builder (don't build, but we might import and/or publish)
  dynamic "source" {
    for_each = { for b, c in local.configs:
        b => c if !contains(c.actions, "build")
      }
    iterator = B
    labels = ["null.alpine"]
    content {
      name = B.key
    }
  }

  ## build provisioners

  # install setup files
  dynamic "provisioner" {
    for_each = { for b, c in local.configs:
        b => c if contains(c.actions, "build")
      }
    iterator = B
    labels = ["file"]
    content {
      only = [ "${B.value.builder}.${B.key}" ]  # configs specific to one build

      sources     = [ for d in B.value.script_dirs: "work/scripts/${d}" ]
      destination = "/tmp/"
    }
  }

  # run setup scripts
  dynamic "provisioner" {
    for_each = { for b, c in local.configs:
        b => c if contains(c.actions, "build")
      }
    iterator = B
    labels = ["shell"]
    content {
      only = [ "${B.value.builder}.${B.key}" ]  # configs specific to one build

      scripts = [ for s in B.value.scripts: "work/scripts/${s}" ]
      use_env_var_file = true
      environment_vars = [
        "DEBUG=${var.DEBUG}",
        "ARCH=${B.value.arch}",
        "BOOTLOADER=${B.value.bootloader}",
        "BUILD_NAME=${B.value.name}",
        "BUILD_REVISION=${B.value.revision}",
        "CLOUD=${B.value.cloud}",
        "END_OF_LIFE=${B.value.end_of_life}",
        "FILESYSTEM=${B.value.filesystem}",
        "FIRMWARE=${B.value.firmware}",
        "IMAGE_LOGIN=${B.value.login}",
        "INITFS_FEATURES=${B.value.initfs_features}",
        "KERNEL_MODULES=${B.value.kernel_modules}",
        "KERNEL_OPTIONS=${B.value.kernel_options}",
        "MOTD=${B.value.motd}",
        "PACKAGES_ADD=${B.value.packages.add}",
        "PACKAGES_DEL=${B.value.packages.del}",
        "PACKAGES_NOSCRIPTS=${B.value.packages.noscripts}",
        "RELEASE=${B.value.release}",
        "REPOS=${B.value.repos}",
        "SERVICES_ENABLE=${B.value.services.enable}",
        "SERVICES_DISABLE=${B.value.services.disable}",
        "VERSION=${B.value.version}",
      ]
    }
  }

  ## build post-processor

  # import and/or publish cloud images
  dynamic "post-processor" {
    for_each = { for b, c in local.configs:
       b => c if contains(c.actions, "import") || contains(c.actions, "publish")
    }
    iterator = B
    labels = ["shell-local"]
    content {
      only = [ "${B.value.builder}.${B.key}", "null.${B.key}" ]
      inline = [ for action in ["import", "publish"]:
        "./cloud_helper.py ${action} ${local.debug_arg} ${local.broker_arg} ${B.key}" if contains(B.value.actions, action)
      ]
    }
  }
}
