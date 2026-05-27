terraform {
  required_version = ">= 1.5"

  backend "s3" {
    bucket         = "gnome-fastly-terraform-state"
    key            = "fastly-gnome-domains/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "gnome-fastly-terraform-lock"
    encrypt        = true
  }

  required_providers {
    fastly = {
      source  = "fastly/fastly"
      version = "~> 5.0"
    }
  }
}
