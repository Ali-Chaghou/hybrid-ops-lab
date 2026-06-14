variable "queue_name" {
  description = "Name of the SQS queue"
  type        = string
}

variable "visibility_timeout_seconds" {
  description = "Time a message stays hidden after being received"
  type        = number
  default     = 30
}

variable "message_retention_seconds" {
  description = "How long messages are retained in the queue"
  type        = number
  default     = 345600
}

variable "sqs_managed_sse_enabled" {
  description = "Enable server-side encryption with SQS-managed keys (SSE-SQS)"
  type        = bool
  default     = true
}
