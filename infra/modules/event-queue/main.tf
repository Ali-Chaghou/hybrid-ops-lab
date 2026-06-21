# Standard (non-FIFO) Movement-Queue mit nativer Dead-Letter-/Redrive-Policy.
# At-least-once-Zustellung; Reihenfolge ist NICHT garantiert (kein FIFO).
# Die DLQ faengt Nachrichten, die nach max_receive_count Zustellungen weiterhin
# nicht verarbeitet werden konnten (Poison Messages), per nativer SQS-Redrive-Policy
# ab — die Anwendung verschiebt NICHTS manuell.

# Dead-Letter-Queue: eigene, lange Retention zum Inspizieren/Replayen.
resource "aws_sqs_queue" "dlq" {
  name                      = "${var.queue_name}-dlq"
  message_retention_seconds = var.dlq_message_retention_seconds
  # Server-Side-Encryption mit SQS-managed Keys (kein KMS noetig); secure by default.
  sqs_managed_sse_enabled = var.sqs_managed_sse_enabled
}

# Haupt-Queue mit Redrive-Policy auf die DLQ.
resource "aws_sqs_queue" "this" {
  name                       = var.queue_name
  visibility_timeout_seconds = var.visibility_timeout_seconds
  message_retention_seconds  = var.message_retention_seconds
  # Server-Side-Encryption mit SQS-managed Keys (kein KMS noetig); secure by default.
  sqs_managed_sse_enabled = var.sqs_managed_sse_enabled

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}

# Erlaubt der DLQ ausschliesslich, Nachrichten aus genau dieser Main-Queue
# aufzunehmen (defensive Redrive-Allow-Policy auf der DLQ-Seite).
resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.this.arn]
  })
}
