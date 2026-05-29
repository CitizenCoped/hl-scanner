variable "region" {
  type = string
}

variable "account_id" {
  type = string
}

variable "ssh_public_key" {
  type = string
}

variable "my_ip" {
  type = string
}

variable "notification_email" {
  type    = string
  default = ""
}
