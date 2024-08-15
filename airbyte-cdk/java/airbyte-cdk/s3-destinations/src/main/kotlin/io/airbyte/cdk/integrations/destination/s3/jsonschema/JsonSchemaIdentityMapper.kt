/*
 * Copyright (c) 2024 Airbyte, Inc., all rights reserved.
 */

package io.airbyte.cdk.integrations.destination.s3.jsonschema

import com.fasterxml.jackson.databind.node.ObjectNode
import io.airbyte.commons.jackson.MoreMappers

open class JsonSchemaIdentityMapper : JsonSchemaMapper() {

    override fun mapNull(schema: ObjectNode): ObjectNode {
        return schema.deepCopy()
    }

    override fun mapObjectWithProperties(schema: ObjectNode): ObjectNode {
        val newSchema = MoreMappers.initMapper().createObjectNode()
        val newProperties = MoreMappers.initMapper().createObjectNode()

        newSchema.put("type", "object")
        schema["properties"].fields().forEach { (key, value) ->
            newProperties.set<ObjectNode>(key, mapSchema(value as ObjectNode))
        }
        newSchema.replace("properties", newProperties)

        return newSchema
    }

    override fun mapObjectWithoutProperties(schema: ObjectNode): ObjectNode {
        return schema.deepCopy()
    }

    override fun mapArrayWithItems(schema: ObjectNode): ObjectNode {
        val newSchema = MoreMappers.initMapper().createObjectNode()
        val newItems = MoreMappers.initMapper().createArrayNode()

        newSchema.put("type", "array")
        schema["items"].elements().forEach { newItems.add(mapSchema(it as ObjectNode)) }
        newSchema.replace("items", newItems)

        return newSchema
    }

    override fun mapArrayWithItem(schema: ObjectNode): ObjectNode {
        val newSchema = MoreMappers.initMapper().createObjectNode()
        val newItem = mapSchema(schema["items"] as ObjectNode)

        newSchema.put("type", "array")
        newSchema.replace("items", newItem)

        return newSchema
    }

    override fun mapArrayWithoutItems(schema: ObjectNode): ObjectNode {
        return schema.deepCopy()
    }

    override fun mapDate(schema: ObjectNode): ObjectNode {
        return schema.deepCopy()
    }

    override fun mapTimeWithoutTimezone(schema: ObjectNode): ObjectNode {
        return schema.deepCopy()
    }

    override fun mapTimeWithTimezone(schema: ObjectNode): ObjectNode {
        return schema.deepCopy()
    }

    override fun mapDateTimeWithTimezone(schema: ObjectNode): ObjectNode {
        return schema.deepCopy()
    }

    override fun mapDateTimeWithoutTimezone(schema: ObjectNode): ObjectNode {
        return schema.deepCopy()
    }

    override fun mapString(schema: ObjectNode): ObjectNode {
        return schema.deepCopy()
    }

    override fun mapBinaryData(schema: ObjectNode): ObjectNode {
        return schema.deepCopy()
    }

    override fun mapBoolean(schema: ObjectNode): ObjectNode {
        return schema.deepCopy()
    }

    override fun mapInteger(schema: ObjectNode): ObjectNode {
        return schema.deepCopy()
    }

    override fun mapNumber(schema: ObjectNode): ObjectNode {
        return schema.deepCopy()
    }

    override fun mapCombined(schema: ObjectNode): ObjectNode {
        // This isn't a perfect identity, because not all types can be represented as a string
        val newCombinedSchema = MoreMappers.initMapper().createObjectNode()
        val newOptions = MoreMappers.initMapper().createArrayNode()

        schema["type"].elements().forEach {
            val newTypeObj = MoreMappers.initMapper().createObjectNode()
            newTypeObj.put("type", it.asText())

            val newOption = mapSchema(newTypeObj)
            newOptions.add(newOption)
        }

        newCombinedSchema.replace("oneOf", newOptions)

        return newCombinedSchema
    }

    override fun mapUnion(schema: ObjectNode): ObjectNode {
        val newUnionSchema = MoreMappers.initMapper().createObjectNode()
        val newOptions = MoreMappers.initMapper().createArrayNode()

        schema["oneOf"].elements().forEach {
            val newOption = mapSchema(it as ObjectNode)
            newOptions.add(newOption)
        }
        newUnionSchema.replace("oneOf", newOptions)

        return newUnionSchema
    }
}