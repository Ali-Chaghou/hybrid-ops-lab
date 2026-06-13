variable "proxmox_endpoint" {
  description = "Proxmox API endpoint, e.g. https://host:8006"
  type        = string
}

variable "proxmox_api_token" {
  description = "Proxmox API token in the form user@realm!tokenname=secret"
  type        = string
  sensitive   = true
}

variable "proxmox_insecure" {
  description = "Skip TLS verification (self-signed cert on the Proxmox host)"
  type        = bool
  default     = true
}

variable "node_name" {
  description = "Proxmox node to deploy on"
  type        = string
}

variable "template_vm_id" {
  description = "VM ID of the Ubuntu template to clone"
  type        = number
  default     = 5000
}

variable "datastore_id" {
  description = "Datastore for VM disks"
  type        = string
}

variable "vm_username" {
  description = "Default user created via cloud-init"
  type        = string
  default     = "ops"
}

variable "vm_ssh_public_keys" {
  description = "SSH public keys for the default user (one per device)"
  type        = list(string)
}

variable "vm_ip_configs" {
  description = "Static IP config per site, CIDR notation"
  type = map(object({
    address = string
    gateway = string
  }))
}
