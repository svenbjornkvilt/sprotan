terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
}

provider "azurerm" {
  features {}
}

variable "location" {
  default = "northeurope"
}

variable "prefix" {
  default = "sprotan"
}

# Resource Group
resource "azurerm_resource_group" "main" {
  name     = "${var.prefix}-rg"
  location = var.location
}

# Container Registry
resource "azurerm_container_registry" "main" {
  name                = "${var.prefix}acr"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Standard"
  admin_enabled       = true
}

# Container Instance with Caddy HTTPS sidecar
resource "azurerm_container_group" "main" {
  name                = "${var.prefix}-mcp"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  os_type             = "Linux"

  image_registry_credential {
    server   = azurerm_container_registry.main.login_server
    username = azurerm_container_registry.main.admin_username
    password = azurerm_container_registry.main.admin_password
  }

  # MCP Server
  container {
    name   = "sprotan-mcp"
    image  = "${azurerm_container_registry.main.login_server}/sprotan-mcp:latest"
    cpu    = "1"
    memory = "2"

    ports {
      port     = 8080
      protocol = "TCP"
    }

    commands = ["python3", "mcp_server.py", "--transport", "http"]
  }

  # Caddy HTTPS reverse proxy
  container {
    name   = "caddy"
    image  = "caddy:2-alpine"
    cpu    = "0.5"
    memory = "0.5"

    ports {
      port     = 443
      protocol = "TCP"
    }

    ports {
      port     = 80
      protocol = "TCP"
    }

    commands = [
      "caddy", "reverse-proxy",
      "--from", "${var.prefix}.northeurope.azurecontainer.io",
      "--to", "localhost:8080"
    ]
  }

  ip_address_type = "Public"
  dns_name_label  = var.prefix

  tags = {
    environment = "production"
    project     = "sprotan"
  }
}

# Outputs
output "mcp_url" {
  value = "https://${azurerm_container_group.main.fqdn}/mcp"
}

output "acr_login_server" {
  value = azurerm_container_registry.main.login_server
}
