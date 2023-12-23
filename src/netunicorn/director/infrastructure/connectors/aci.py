from __future__ import annotations

import asyncio
from typing import Optional, NoReturn, Tuple, Iterable, Any

import yaml
import logging

import os

from azure.identity import ClientSecretCredential
from azure.mgmt.containerinstance import ContainerInstanceManagementClient
from azure.mgmt.containerinstance.models import ContainerGroup
from netunicorn.base.architecture import Architecture

from netunicorn.base.deployment import Deployment
from netunicorn.base.environment_definitions import DockerImage
from netunicorn.base.nodes import Node, UncountableNodePool
from returns.result import Result, Success, Failure

from netunicorn.director.base.connectors.protocol import (
    NetunicornConnectorProtocol,
)
from netunicorn.director.base.connectors.types import StopExecutorRequest


class AzureContainerInstances(NetunicornConnectorProtocol):
    def __init__(
        self,
        connector_name: str,
        config_file: str | None,
        netunicorn_gateway: str,
        logger: Optional[logging.Logger] = None,
    ):
        self.connector_name = connector_name
        self.netunicorn_gateway = netunicorn_gateway

        if config_file is not None:
            with open(config_file, "r") as f:
                self.config = yaml.safe_load(f)

        self.azure_tenant_id = (
            os.environ.get("NETUNICORN_AZURE_TENANT_ID", None)
            or self.config["netunicorn.azure.tenant.id"]
        )
        self.azure_client_id = (
            os.environ.get("NETUNICORN_AZURE_CLIENT_ID", None)
            or self.config["netunicorn.azure.client.id"]
        )
        self.azure_client_secret = (
            os.environ.get("NETUNICORN_AZURE_CLIENT_SECRET", None)
            or self.config["netunicorn.azure.client.secret"]
        )
        self.subscription_id = (
            os.environ.get("NETUNICORN_AZURE_SUBSCRIPTION_ID", None)
            or self.config["netunicorn.azure.subscription.id"]
        )
        self.resource_group_name = (
            os.environ.get("NETUNICORN_AZURE_RESOURCE_GROUP", None)
            or self.config["netunicorn.azure.resource.group"]
        )
        self.container_location = (
            os.environ.get("NETUNICORN_AZURE_LOCATION", None)
            or self.config["netunicorn.azure.location"]
        )

        self.netunicorn_access_tags = self.config["netunicorn.azure.access.tags"]
        self.soft_limit = self.config["netunicorn.azure.soft_limit"]

        # noinspection PyTypeChecker
        self.client = ContainerInstanceManagementClient(
            credential=ClientSecretCredential(
                tenant_id=self.azure_tenant_id,
                client_id=self.azure_client_id,
                client_secret=self.azure_client_secret,
            ),
            subscription_id=self.subscription_id,
        )

        if not logger:
            logging.basicConfig()
            logger = logging.getLogger(__name__)
            logger.setLevel(logging.INFO)

        self.logger = logger

    async def __cleaner(self) -> NoReturn:
        """
        This is a backup cleaner that will delete all container groups that are not running.
        These groups should be deleted by cleanup, but something can go wrong and we want
        to avoid having orphaned container groups.
        """
        self.logger.info("Starting Azure Container Instances cleaner")
        while True:
            try:
                container_groups: Iterable[
                    ContainerGroup
                ] = self.client.container_groups.list_by_resource_group(
                    self.resource_group_name
                )

                # if all containers in the group are not running, delete the group
                for group in container_groups:
                    group_name: str = group.name  # type: ignore
                    group_containers: list[Container] = group.containers  # type: ignore
                    # https://github.com/Azure/azure-rest-api-specs/issues/21280
                    group = self.client.container_groups.get(
                        self.resource_group_name, group_name
                    )
                    group_is_running = False
                    for container in group_containers:
                        if container.instance_view.current_state.state == "Running":
                            group_is_running = True
                            break

                    if not group_is_running:
                        self.logger.info(f"Deleting container group {group.name}")
                        self.client.container_groups.begin_delete(
                            resource_group_name=self.resource_group_name,
                            container_group_name=group_name,
                        ).result()

            except Exception as e:
                self.logger.error(f"Error while cleaning container groups: {e}")
            await asyncio.sleep(30)

    async def initialize(self) -> None:
        await asyncio.create_task(self.__cleaner())

    async def health(self) -> Tuple[bool, str]:
        return True, "Cannot check if Azure Container Instances is healthy"

    async def shutdown(self) -> None:
        pass

    async def get_nodes(
            self,
            username: str,
            authentication_context: Optional[dict[str, str]] = None,
            *args: Any,
            **kwargs: Any,
    ) -> UncountableNodePool:
        available_node_types = [
            Node(
                name=f"aci-{username}-",
                architecture=Architecture.LINUX_AMD64,
                properties={
                    "memory_in_gb": 1,
                    "cpu": 1,
                    "netunicorn-environments": {"DockerImage"},
                    "netunicorn-access-tags": self.netunicorn_access_tags,
                },
            )
        ]
        return UncountableNodePool(node_template=available_node_types, soft_limit=self.soft_limit)

    async def deploy(
        self,
        username: str,
        experiment_id: str,
        deployments: list[Deployment],
        deployment_context: Optional[dict[str, str]],
        authentication_context: Optional[dict[str, str]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Result[Optional[str], str]]:
        """
        Azure Container Instances automatically starts the container when it is created
        (seriously: https://stackoverflow.com/questions/67385581/deploying-azure-container-s-without-running-them),
        so this function only checks that all deployments are of DockerImage type,
        as it is the only type supported by Azure Container Instances.
        """

        result: dict[str, Result[Optional[str], str]] = {}
        for deployment in deployments:
            if not deployment.prepared:
                result[deployment.executor_id] = Failure("Deployment is not prepared")
                continue
            if not isinstance(deployment.environment_definition, DockerImage):
                result[deployment.executor_id] = Failure(
                    "Azure Container Instances only supports DockerImage deployments"
                )
                continue
            if deployment.node.architecture != Architecture.LINUX_AMD64:
                result[deployment.executor_id] = Failure(
                    "Azure Container Instances only supports Linux AMD64 nodes"
                )
                continue
            result[deployment.executor_id] = Success(None)

        return result

    async def execute(
        self,
        username: str,
        experiment_id: str,
        deployments: list[Deployment],
        execution_context: Optional[dict[str, str]],
        authentication_context: Optional[dict[str, str]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Result[Optional[str], str]]:
        container_groups: dict[str, dict[str, Any]] = {}
        for deployment in deployments:
            if not isinstance(deployment.environment_definition, DockerImage):
                continue
            deployment.environment_definition.runtime_context.environment_variables[
                "NETUNICORN_EXECUTOR_ID"
            ] = deployment.executor_id
            deployment.environment_definition.runtime_context.environment_variables[
                "NETUNICORN_GATEWAY_ENDPOINT"
            ] = self.netunicorn_gateway
            deployment.environment_definition.runtime_context.environment_variables[
                "NETUNICORN_EXPERIMENT_ID"
            ] = experiment_id
            environment_variables = [
                {"name": x, "value": y}
                for x, y in deployment.environment_definition.runtime_context.environment_variables.items()
            ]

            container_groups[deployment.executor_id] = {
                "location": self.container_location,
                "restart_policy": "Never",
                "os_type": "Linux",
                "containers": [
                    {
                        "name": deployment.executor_id,
                        "image": deployment.environment_definition.image,
                        "environment_variables": environment_variables,
                        "resources": {
                            "requests": {
                                "memory_in_gb": deployment.node.properties.get(
                                    "memory_in_gb", 1
                                ),
                                "cpu": deployment.node.properties.get("cpu", 1),
                            }
                        },
                    }
                ],
            }

        self.logger.info(f"Creating container groups: {container_groups}")

        # noinspection PyTypeChecker
        # trust me
        values: tuple[Exception | Result[None, str], ...] = await asyncio.gather(
            *[
                self._create_container_group(key, value) for key, value in container_groups.items()  # type: ignore
            ],
            return_exceptions=True,
        )

        results = {}
        for i, key in enumerate(container_groups.keys()):
            value: Exception | Result[Optional[str], str] = values[i]
            if isinstance(value, Exception):
                value = Failure(str(value))
            results[key] = value

        return results

    async def _create_container_group(
        self, executor_id: str, group: ContainerGroup
    ) -> Result[None, str]:
        self.logger.debug(f"Creating container group {executor_id}")
        loop = asyncio.get_running_loop()
        try:
            request = self.client.container_groups.begin_create_or_update(
                resource_group_name=self.resource_group_name,
                container_group_name=executor_id,
                container_group=group,
            )
            await loop.run_in_executor(None, request.result)
            return Success(None)
        except Exception as e:
            self.logger.error(f"Error while creating container group: {e}")
            return Failure(str(e))

    async def stop_executors(
        self,
        username: str,
        requests_list: list[StopExecutorRequest],
        cancellation_context: Optional[dict[str, str]],
        authentication_context: Optional[dict[str, str]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Result[Optional[str], str]]:
        self.logger.warning("Stop executors called, but not implemented")
        return {
            request["executor_id"]: Failure("Stop executor is not implemented")
            for request in requests_list
        }

    async def cleanup(
        self,
        experiment_id: str,
        deployments: list[Deployment],
        *args: Any,
        **kwargs: Any,
    ) -> None:

        for deployment in deployments:
            # try to delete the container group
            try:
                self.client.container_groups.begin_delete(
                    resource_group_name=self.resource_group_name,
                    container_group_name=deployment.executor_id,
                )
            except Exception as e:
                self.logger.error(f"Error while deleting container group: {e}")
