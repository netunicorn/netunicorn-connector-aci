# Azure Container Instances connector for netunicorn
This is a connector for Microsoft Azure Container Instances for netunicorn.

## Installation
```bash
pip install netunicorn-connector-aci
```

## Usage
Create a configuration file and provide needed parameters (see [example](configuration-example.yaml)).
Then, add information about this connector to the main configuration file of netunicorn-director-infrastructure.

All of these parameters are required and to be provided by Azure cloud user.

P.S.: Most likely you also want to submit a help ticket to Azure cloud to increase your containers quota
for your region as the default one is pretty low.