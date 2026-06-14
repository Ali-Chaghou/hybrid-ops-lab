# event-queue

Wiederverwendbares OpenTofu-Modul für eine SQS-Queue. AWS-portabel; Visibility-Timeout
und Retention sind parametrierbar, Server-Side-Encryption (SSE-SQS) ist standardmäßig
aktiv. Zur lokalen Verwendung gegen ElasticMQ siehe
[`../../environments/cloud`](../../environments/cloud) und
[ADR-005](../../../docs/decisions/005-elasticmq-statt-localstack.md).

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.12.0 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | ~> 6.0 |

## Providers

| Name | Version |
| ---- | ------- |
| <a name="provider_aws"></a> [aws](#provider\_aws) | 6.50.0 |

## Modules

No modules.

## Resources

| Name | Type |
| ---- | ---- |
| [aws_sqs_queue.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sqs_queue) | resource |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_message_retention_seconds"></a> [message\_retention\_seconds](#input\_message\_retention\_seconds) | How long messages are retained in the queue | `number` | `345600` | no |
| <a name="input_queue_name"></a> [queue\_name](#input\_queue\_name) | Name of the SQS queue | `string` | n/a | yes |
| <a name="input_sqs_managed_sse_enabled"></a> [sqs\_managed\_sse\_enabled](#input\_sqs\_managed\_sse\_enabled) | Enable server-side encryption with SQS-managed keys (SSE-SQS) | `bool` | `true` | no |
| <a name="input_visibility_timeout_seconds"></a> [visibility\_timeout\_seconds](#input\_visibility\_timeout\_seconds) | Time a message stays hidden after being received | `number` | `30` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_queue_arn"></a> [queue\_arn](#output\_queue\_arn) | ARN of the created SQS queue |
| <a name="output_queue_name"></a> [queue\_name](#output\_queue\_name) | Name of the created SQS queue |
| <a name="output_queue_url"></a> [queue\_url](#output\_queue\_url) | URL of the created SQS queue |
<!-- END_TF_DOCS -->
