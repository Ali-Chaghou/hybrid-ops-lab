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

variable "max_receive_count" {
  description = <<-EOT
    Number of times a message may be received before SQS redrives it to the DLQ.
    Chosen at 5: high enough that a short transient failure (DB blip / brief queue
    hiccup) does not immediately dead-letter a message, low enough that a truly
    unprocessable (poison) message leaves the main queue after a bounded number of
    visibility cycles instead of being redelivered forever.
  EOT
  type        = number
  default     = 5

  validation {
    condition     = var.max_receive_count >= 2 && var.max_receive_count <= 100
    error_message = "max_receive_count must be between 2 and 100 (neither hair-trigger nor effectively unbounded)."
  }
}

variable "dlq_message_retention_seconds" {
  description = "Retention for the dead-letter queue. Default 14 days (AWS maximum) so poison messages can be inspected/replayed long after they arrive."
  type        = number
  default     = 1209600
}
