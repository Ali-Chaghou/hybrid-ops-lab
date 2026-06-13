locals {
  sites = {
    hol-site-dc = {
      vm_id   = 8001
      cores   = 2
      memory  = 4096
      disk_gb = 20
    }
    hol-site-cloud = {
      vm_id   = 8002
      cores   = 4
      memory  = 8192
      disk_gb = 40
    }
  }
}

resource "proxmox_virtual_environment_vm" "site" {
  for_each = local.sites

  name      = each.key
  vm_id     = each.value.vm_id
  node_name = var.node_name
  started   = true

  clone {
    vm_id = var.template_vm_id
    full  = true
  }

  cpu {
    cores = each.value.cores
  }

  memory {
    dedicated = each.value.memory
  }

  disk {
    datastore_id = var.datastore_id
    interface    = "scsi0"
    size         = each.value.disk_gb
  }

  initialization {
    datastore_id = var.datastore_id

    ip_config {
      ipv4 {
        address = var.vm_ip_configs[each.key].address
        gateway = var.vm_ip_configs[each.key].gateway
      }
    }

    user_account {
      username = var.vm_username
      keys     = var.vm_ssh_public_keys
    }
  }
}
