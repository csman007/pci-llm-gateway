terraform {
  backend "s3" {
    bucket         = "pci-llm-gateway-tfstate"
    key            = "terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "pci-llm-gateway-tfstate-lock"
  }
}
