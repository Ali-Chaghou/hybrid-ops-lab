# proxmox

OpenTofu-Konfiguration zur VM-Provisionierung auf Proxmox: beide Standort-VMs
(`hol-site-dc`, `hol-site-cloud`) werden per `for_each` aus einem Ubuntu-Template
geklont, cloud-init setzt statische IP, Benutzer und SSH-Keys. API-Token, IPs und
Keys liegen in gitignorten `terraform.tfvars` (Vorlage: `terraform.tfvars.example`).
Hintergrund: [ADR-004](../../docs/decisions/004-proxmox-provisionierung.md).

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.12.0 |
| <a name="requirement_proxmox"></a> [proxmox](#requirement\_proxmox) | ~> 0.107 |

## Providers

| Name | Version |
| ---- | ------- |
| <a name="provider_proxmox"></a> [proxmox](#provider\_proxmox) | 0.109.0 |

## Modules

No modules.

## Resources

| Name | Type |
| ---- | ---- |
| [proxmox_virtual_environment_vm.site](https://registry.terraform.io/providers/bpg/proxmox/latest/docs/resources/virtual_environment_vm) | resource |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_datastore_id"></a> [datastore\_id](#input\_datastore\_id) | Datastore for VM disks | `string` | n/a | yes |
| <a name="input_node_name"></a> [node\_name](#input\_node\_name) | Proxmox node to deploy on | `string` | n/a | yes |
| <a name="input_proxmox_api_token"></a> [proxmox\_api\_token](#input\_proxmox\_api\_token) | Proxmox API token in the form user@realm!tokenname=secret | `string` | n/a | yes |
| <a name="input_proxmox_endpoint"></a> [proxmox\_endpoint](#input\_proxmox\_endpoint) | Proxmox API endpoint, e.g. https://host:8006 | `string` | n/a | yes |
| <a name="input_proxmox_insecure"></a> [proxmox\_insecure](#input\_proxmox\_insecure) | Skip TLS verification (self-signed cert on the Proxmox host) | `bool` | `true` | no |
| <a name="input_template_vm_id"></a> [template\_vm\_id](#input\_template\_vm\_id) | VM ID of the Ubuntu template to clone | `number` | `5000` | no |
| <a name="input_vm_ip_configs"></a> [vm\_ip\_configs](#input\_vm\_ip\_configs) | Static IP config per site, CIDR notation | <pre>map(object({<br/>    address = string<br/>    gateway = string<br/>  }))</pre> | n/a | yes |
| <a name="input_vm_ssh_public_keys"></a> [vm\_ssh\_public\_keys](#input\_vm\_ssh\_public\_keys) | SSH public keys for the default user (one per device) | `list(string)` | n/a | yes |
| <a name="input_vm_username"></a> [vm\_username](#input\_vm\_username) | Default user created via cloud-init | `string` | `"ops"` | no |

## Outputs

No outputs.
<!-- END_TF_DOCS -->
