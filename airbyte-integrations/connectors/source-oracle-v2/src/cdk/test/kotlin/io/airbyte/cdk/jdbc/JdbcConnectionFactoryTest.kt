/*
 * Copyright (c) 2024 Airbyte, Inc., all rights reserved.
 */

package io.airbyte.cdk.jdbc

import io.airbyte.cdk.ssh.SshBastionContainer
import io.airbyte.cdk.test.source.TestSourceConfigurationFactory
import io.airbyte.cdk.test.source.TestSourceConfigurationJsonObject
import io.airbyte.cdk.testcontainers.DOCKER_HOST_FROM_WITHIN_CONTAINER
import org.junit.jupiter.api.Assertions
import org.junit.jupiter.api.Test
import org.testcontainers.Testcontainers

class JdbcConnectionFactoryTest {

    val h2 = H2TestFixture()
    init {
        Testcontainers.exposeHostPorts(h2.port)
    }
    val sshBastion = SshBastionContainer(tunnelingToHostPort = h2.port)

    @Test
    fun testVanilla() {
        val configPojo =
            TestSourceConfigurationJsonObject().apply {
                port = h2.port
                database = h2.database
            }
        val factory = JdbcConnectionFactory(TestSourceConfigurationFactory().make(configPojo))
        Assertions.assertEquals("H2", factory.get().metaData.databaseProductName)
    }

    @Test
    fun testSshKeyAuth() {
        val configPojo =
            TestSourceConfigurationJsonObject().apply {
                host = DOCKER_HOST_FROM_WITHIN_CONTAINER // required only because of container
                port = h2.port
                database = h2.database
                setTunnelMethodValue(sshBastion.outerKeyAuthTunnelMethod)
            }
        val factory = JdbcConnectionFactory(TestSourceConfigurationFactory().make(configPojo))
        Assertions.assertEquals("H2", factory.get().metaData.databaseProductName)
    }

    @Test
    fun testSshPasswordAuth() {
        val configPojo =
            TestSourceConfigurationJsonObject().apply {
                host = DOCKER_HOST_FROM_WITHIN_CONTAINER // required only because of container
                port = h2.port
                database = h2.database
                setTunnelMethodValue(sshBastion.outerPasswordAuthTunnelMethod)
            }
        val factory = JdbcConnectionFactory(TestSourceConfigurationFactory().make(configPojo))
        Assertions.assertEquals("H2", factory.get().metaData.databaseProductName)
    }
}
