resource "aws_sqs_queue" "this" {
  name                       = var.queue_name
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = var.message_retention_seconds
  # Server-Side-Encryption mit SQS-managed Keys (kein KMS noetig); secure by default.
  sqs_managed_sse_enabled = var.sqs_managed_sse_enabled
}
