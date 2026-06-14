# environments/cloud

Validiert das Modul `event-queue` als AWS-portables IaC (`tofu validate` / `tofu plan`).

Lokal wird **kein** `tofu apply` gegen ElasticMQ ausgeführt: Der AWS-Provider
pollt nach dem Anlegen `GetQueueAttributes` und vergleicht den vollständigen
Attributsatz, den ein Emulator nicht deckungsgleich liefert (Timeout 'notequal').
Die lokale Queue wird stattdessen deklarativ in `sites/cloud/elasticmq.conf`
bereitgestellt. `apply` zielt auf echtes AWS.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.12.0 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | ~> 6.0 |

## Providers

No providers.

## Modules

| Name | Source | Version |
| ---- | ------ | ------- |
| <a name="module_event_queue"></a> [event\_queue](#module\_event\_queue) | ../../modules/event-queue | n/a |

## Resources

No resources.

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_aws_region"></a> [aws\_region](#input\_aws\_region) | AWS region reported to the SQS endpoint | `string` | `"eu-central-1"` | no |
| <a name="input_queue_name"></a> [queue\_name](#input\_queue\_name) | Name of the SQS queue to create | `string` | `"inventory-movements"` | no |
| <a name="input_sqs_endpoint_url"></a> [sqs\_endpoint\_url](#input\_sqs\_endpoint\_url) | SQS-compatible endpoint (ElasticMQ on site-cloud) | `string` | n/a | yes |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_queue_arn"></a> [queue\_arn](#output\_queue\_arn) | ARN of the SQS queue |
| <a name="output_queue_url"></a> [queue\_url](#output\_queue\_url) | URL of the SQS queue |
<!-- END_TF_DOCS -->
