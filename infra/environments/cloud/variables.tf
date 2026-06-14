variable "aws_region" {
  description = "AWS region reported to the SQS endpoint"
  type        = string
  default     = "eu-central-1"
}

variable "sqs_endpoint_url" {
  description = "SQS-compatible endpoint (ElasticMQ on site-cloud)"
  type        = string
}

variable "queue_name" {
  description = "Name of the SQS queue to create"
  type        = string
  default     = "inventory-movements"
}
