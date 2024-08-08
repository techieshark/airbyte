#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, List, Set, Union

import dpath
import google.generativeai as genai
from airbyte_protocol.models import AirbyteCatalog, AirbyteStream
from dagger import Container
from markdown_it import MarkdownIt
from pipelines.airbyte_ci.connectors.build_image.steps.python_connectors import BuildConnectorImages
from pipelines.airbyte_ci.connectors.consts import CONNECTOR_TEST_STEP_ID
from pipelines.airbyte_ci.connectors.context import ConnectorContext, PipelineContext
from pipelines.airbyte_ci.connectors.reports import Report
from pipelines.consts import LOCAL_BUILD_PLATFORM
from pipelines.helpers.connectors.command import run_connector_steps
from pipelines.helpers.execution.run_steps import STEP_TREE, StepToRun
from pipelines.models.steps import Step, StepResult, StepStatus
from pydbml import Database
from pydbml.classes import Column, Index, Reference, Table
from pydbml.renderer.dbml.default import DefaultDBMLRenderer

if TYPE_CHECKING:
    from anyio import Semaphore

# TODO: pass secret in dagger?
API_KEY = "API_KEY"
genai.configure(api_key=API_KEY)

# TODO: pass secret in dagger
DBDOCS_TOKEN = "TOKEN"


class GenerateErdSchema(Step):
    context: ConnectorContext

    title = "Generate ERD schema using Gemini LLM"

    def __init__(self, context: PipelineContext) -> None:
        super().__init__(context)
        self._model = genai.GenerativeModel("gemini-1.5-flash")

    async def _run(self, connector_to_discover: Container) -> StepResult:
        connector = self.context.connector
        python_path = connector.code_directory
        file_path = Path(os.path.abspath(os.path.join(python_path)))
        IN_CONTAINER_CONFIG_PATH = "/data/config.json"
        config_secret = open(file_path / "secrets" / "config.json").read()
        discover_output = (
            await connector_to_discover.with_new_file(IN_CONTAINER_CONFIG_PATH, contents=config_secret)
            .with_exec(["discover", "--config", IN_CONTAINER_CONFIG_PATH])
            .stdout()
        )
        configured_catalog = self._get_schema_from_discover_output(discover_output)

        json.dump(configured_catalog, open(file_path / "configured_catalog.json", "w"), indent=4)

        normalized_catalog = self._normalize_schema_catalog(configured_catalog)
        erd_relations_schema = self._get_relations_from_gemini(source_name=connector.name, catalog=normalized_catalog)
        clean_schema = self._remove_non_existing_relations(configured_catalog, erd_relations_schema)

        # save ERD to source directory
        json.dump(clean_schema, open(file_path / "erd.json", "w"), indent=4)

        return StepResult(step=self, status=StepStatus.SUCCESS)

    @staticmethod
    def _get_schema_from_discover_output(discover_output: str):
        """
        :param discover_output:
        :return:
        """
        for line in discover_output.split("\n"):
            json_line = json.loads(line)
            if json_line.get("type") == "CATALOG":
                return json.loads(line).get("catalog")
        raise ValueError("No catalog was found in output")

    @staticmethod
    def _normalize_schema_catalog(schema: dict) -> dict:
        """
        Foreign key cannot be of type object or array, therefore, we can remove these properties.
        :param schema: json_schema in draft7
        :return: json_schema in draft7 with TOP level properties only.
        """
        streams = schema["streams"]
        for stream in streams:
            to_rem = dpath.search(
                stream["json_schema"]["properties"],
                ["**"],
                afilter=lambda x: isinstance(x, dict) and ("array" in str(x.get("type", "")) or "object" in str(x.get("type", ""))),
            )
            for key in to_rem:
                stream["json_schema"]["properties"].pop(key)
        return streams

    def _get_relations_from_gemini(self, source_name: str, catalog: dict) -> dict:
        """

        :param source_name:
        :param catalog:
        :return: {"streams":[{'name': 'ads', 'relations': {'account_id': 'ad_account.id', 'campaign_id': 'campaigns.id', 'adset_id': 'ad_sets.id'}}, ...]}
        """
        system = "You are an Database developer in charge of communicating well to your users."

        source_desc = """
You are working on the {source_name} API service.

The current JSON Schema format is as follows:
{current_schema}, where "streams" has a list of streams, which represents database tables, and list of properties in each, which in turn, represent DB columns. Streams presented in list are the only available ones.
Generate and add a `foreign_key` with reference for each field in top level of properties that is helpful in understanding what the data represents and how are streams related to each other. Pay attention to fields ends with '_id'.
        """.format(
            source_name=source_name, current_schema=catalog
        )
        task = """
Please provide answer in the following format:
{streams: [{"name": "<stream_name>", "relations": {"<foreign_key>": "<ref_table.column_name>"} }]}
Pay extra attention that in <ref_table.column_name>" "ref_table" should be one of the list of streams, and "column_name" should be one of the property in respective reference stream.
Limitations:
- Not all tables should have relations
- Reference should point to 1 table only.
- table cannot reference on itself, on other words, e.g. `ad_account` cannot have relations with "ad_account" as a "ref_table"
        """
        response = self._model.generate_content(f"{system} {source_desc} {task}")
        md = MarkdownIt("commonmark")
        tokens = md.parse(response.text)
        response_json = json.loads(tokens[0].content)
        return response_json

    @staticmethod
    def _remove_non_existing_relations(discovered_catalog_schema: dict, relation_dict: dict) -> dict:
        """LLM can sometimes add non-existing relations, so we need check and remove them"""
        # TODO: filter out non existing field relations
        all_streams_names = [x.get("name") for x in discovered_catalog_schema.get("streams")]
        for stream in relation_dict["streams"]:
            ref_tables = [x.split(".")[0] for x in stream["relations"].values()]
            if non_existing_streams := set(ref_tables) - set(all_streams_names):
                print(f'non_existing_stream was found in {stream["name"]=}: {non_existing_streams}. Removing ...')
                for non_existing_stream in non_existing_streams:
                    keys = dpath.search(stream["relations"], ["**"], afilter=lambda x: x.startswith(non_existing_stream))
                    for key in keys:
                        stream["relations"].pop(key)

        return relation_dict


class GenerateDbmlSchema(Step):
    context: ConnectorContext

    title = "Generate DBML file from discovered catalog and erd_relation"

    def __init__(self, context: PipelineContext) -> None:
        super().__init__(context)

    def _get_catalog(self, catalog_path: str) -> AirbyteCatalog:
        with open(catalog_path, "r") as file:
            try:
                return AirbyteCatalog.parse_obj(json.loads(file.read()))
            except json.JSONDecodeError as error:
                raise ValueError(f"Could not read json file {catalog_path}: {error}. Please ensure that it is a valid JSON.")

    def _get_relationships_by_stream(self, schema_relationships_path: str):
        with open(schema_relationships_path, "r") as file:
            return json.load(file)["streams"]

    def _extract_type(self, property_type: Union[str, List[str]]) -> str:
        if isinstance(property_type, str):
            return property_type

        types = list(property_type)
        if "null" in types:
            # As we flag everything as nullable (except PK and cursor field), there is little value in keeping the information in order to show
            # this in DBML
            types.remove("null")
        if len(types) != 1:
            raise ValueError(f"Expected only one type apart from `null` but got {len(types)}: {property_type}")
        return types[0]

    def _is_pk(self, stream: AirbyteStream, property_name: str) -> bool:
        return stream.source_defined_primary_key == [property_name]

    def _has_composite_key(self, stream: AirbyteStream) -> bool:
        return len(stream.source_defined_primary_key) > 1

    def _get_column(self, database: Database, table_name: str, column_name: str) -> Column:
        matching_tables = list(filter(lambda table: table.name == table_name, database.tables))
        if len(matching_tables) == 0:
            raise ValueError(f"Could not find table {table_name}")
        elif len(matching_tables) > 1:
            raise ValueError(f"Unexpected error: many tables found with name {table_name}")

        table: Table = matching_tables[0]
        matching_columns = list(filter(lambda column: column.name == column_name, table.columns))
        if len(matching_columns) == 0:
            raise ValueError(f"Could not find column {column_name} in table {table_name}. Columns are: {table.columns}")
        elif len(matching_columns) > 1:
            raise ValueError(f"Unexpected error: many columns found with name {column_name} for table {table_name}")

        return matching_columns[0]

    def _get_source_name(self, source_folder: Path) -> str:
        return source_folder.name

    def _get_manifest_path(self, source_folder: Path) -> Path:
        return source_folder / source_folder.name.replace("-", "_") / "manifest.yaml"

    def _get_streams_from_schemas_folder(self, source_folder: Path) -> Set[str]:
        schemas_folder = source_folder / source_folder.name.replace("-", "_") / "schemas"
        return {p.name.replace(".json", "") for p in schemas_folder.iterdir() if p.is_file()}

    def _has_manifest(self, source_folder: Path):
        return self._get_manifest_path(source_folder).exists()

    def _is_dynamic(self, source_folder: Path, stream_name: str) -> bool:
        if self._has_manifest(source_folder):
            raise NotImplementedError()
        return stream_name not in self._get_streams_from_schemas_folder(source_folder)

    async def _run(self, connector_to_discover: Container = None) -> StepResult:
        connector = self.context.connector
        python_path = connector.code_directory
        file_path = Path(os.path.abspath(os.path.join(python_path)))

        catalog = self._get_catalog(str(file_path / "configured_catalog.json"))
        source_folder = python_path
        database = Database()
        for stream in catalog.streams:
            if self._is_dynamic(source_folder, stream.name):
                print(f"Skipping stream {stream.name} as it is dynamic")
                continue

            dbml_table = Table(stream.name)
            for property_name, property_information in stream.json_schema.get("properties").items():
                dbml_table.add_column(
                    Column(
                        name=property_name,
                        type=self._extract_type(property_information["type"]),
                        pk=self._is_pk(stream, property_name),
                    )
                )

            if stream.source_defined_primary_key and len(stream.source_defined_primary_key) > 1:
                if any(map(lambda key: len(key) != 1, stream.source_defined_primary_key)):
                    raise ValueError(f"Does not support nested key as part of primary key `{stream.source_defined_primary_key}`")

                composite_key_columns = [
                    column for key in stream.source_defined_primary_key for column in dbml_table.columns if column.name in key
                ]
                if len(composite_key_columns) < len(stream.source_defined_primary_key):
                    raise ValueError("Unexpected error: missing PK column from dbml table")

                dbml_table.add_index(
                    Index(
                        subjects=composite_key_columns,
                        pk=True,
                    )
                )
            database.add(dbml_table)

        for stream in self._get_relationships_by_stream(str(file_path / "erd.json")):
            for column_name, relationship in stream["relations"].items():
                if self._is_dynamic(source_folder, stream["name"]):
                    print(f"Skipping relationship as stream {stream['name']} from relationship is dynamic")
                    continue

                try:
                    target_table_name, target_column_name = relationship.split(".")
                except ValueError as exception:
                    raise ValueError("If 'too many values to unpack', relationship to nested fields is not supported") from exception

                if self._is_dynamic(source_folder, target_table_name):
                    print(f"Skipping relationship as target stream {target_table_name} is dynamic")
                    continue

                database.add_reference(
                    Reference(
                        type="<>",  # we don't have the information of which relationship type it is so we assume many-to-many for now
                        col1=self._get_column(database, stream["name"], column_name),
                        col2=self._get_column(database, target_table_name, target_column_name),
                    )
                )

        # to publish this dbml file to dbdocs, use `DBDOCS_TOKEN=<token> dbdocs build source.dbml --project=<source>`
        with open(file_path / "source.dbml", "w") as f:
            f.write(DefaultDBMLRenderer.render_db(database))

        return StepResult(step=self, status=StepStatus.SUCCESS)


class UploadDbmlSchema(Step):
    context: ConnectorContext

    title = "Upload DBML file to dbdocs.io"

    def __init__(self, context: PipelineContext) -> None:
        super().__init__(context)

    async def _run(self, connector_to_discover: Container = None) -> StepResult:
        connector = self.context.connector
        python_path = connector.code_directory
        file_path = Path(os.path.abspath(os.path.join(python_path)))
        source_dbml_content = open(file_path / "source.dbml").read()

        dbdocs_container = await (
            self.dagger_client.container()
            .from_("node:lts-bullseye-slim")
            .with_exec(["npm", "install", "-g", "dbdocs"])
            .with_env_variable("DBDOCS_TOKEN", DBDOCS_TOKEN)
            .with_workdir("/airbyte_dbdocs")
            .with_new_file("/airbyte_dbdocs/source.dbml", contents=source_dbml_content)
        )

        db_docs_build = ["dbdocs", "build", "source.dbml", f"--project={connector.technical_name}"]
        await dbdocs_container.with_exec(db_docs_build).stdout()
        # TODO: produce link to dbdocs in output logs

        return StepResult(step=self, status=StepStatus.SUCCESS)


async def run_connector_generate_erd_schema_pipeline(context: ConnectorContext, semaphore: "Semaphore") -> Report:
    context.targeted_platforms = [LOCAL_BUILD_PLATFORM]

    steps_to_run: STEP_TREE = []

    steps_to_run.append([StepToRun(id=CONNECTOR_TEST_STEP_ID.BUILD, step=BuildConnectorImages(context))])

    steps_to_run.append(
        [
            StepToRun(
                id=CONNECTOR_TEST_STEP_ID.AIRBYTE_ERD_GENERATE,
                step=GenerateErdSchema(context),
                args=lambda results: {"connector_to_discover": results[CONNECTOR_TEST_STEP_ID.BUILD].output[LOCAL_BUILD_PLATFORM]},
                depends_on=[CONNECTOR_TEST_STEP_ID.BUILD],
            ),
        ]
    )

    steps_to_run.append(
        [
            StepToRun(
                id=CONNECTOR_TEST_STEP_ID.AIRBYTE_DBML_GENERATE,
                step=GenerateDbmlSchema(context),
                depends_on=[CONNECTOR_TEST_STEP_ID.AIRBYTE_ERD_GENERATE],
            ),
        ]
    )

    steps_to_run.append(
        [
            StepToRun(
                id=CONNECTOR_TEST_STEP_ID.AIRBYTE_DBML_UPLOAD,
                step=UploadDbmlSchema(context),
                depends_on=[CONNECTOR_TEST_STEP_ID.AIRBYTE_DBML_GENERATE],
            ),
        ]
    )

    return await run_connector_steps(context, semaphore, steps_to_run)