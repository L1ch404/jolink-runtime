"""Real MCP regression: Spring Boot's Jupiter extension must not hide JUnit 4 tests."""

import os
from pathlib import Path

import anyio
import pytest
from java_support import open_mcp_session, require_real_mcp_java_e2e, temporary_stderr


POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
<modelVersion>4.0.0</modelVersion>
<parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId>
<version>2.5.2</version><relativePath/></parent>
<groupId>example</groupId><artifactId>http-export-test</artifactId><version>1</version>
<properties><java.version>1.8</java.version></properties>
<dependencies>
<dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>
<dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-test</artifactId><scope>test</scope></dependency>
<dependency><groupId>junit</groupId><artifactId>junit</artifactId><scope>test</scope></dependency>
<dependency><groupId>org.junit.vintage</groupId><artifactId>junit-vintage-engine</artifactId><scope>test</scope></dependency>
</dependencies></project>
"""

ENDPOINT = """package example;
import java.util.Map;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.*;
import org.springframework.web.bind.annotation.*;

@RestController
public class ExportEndpoint {
    public interface ExportService { byte[] export(Map<String, Object> request); }
    public static class SyncService { public void sync() { throw new AssertionError("must be mocked"); } }
    public interface SessionService { String session(); }
    @Autowired private ExportService service;
    @Autowired private SyncService sync;
    @Autowired private SessionService session;
    @PostMapping("/export")
    public ResponseEntity<byte[]> export(@RequestBody Map<String, Object> body) {
        boolean docx = body.containsKey("id");
        return ResponseEntity.ok()
            .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=export." + (docx ? "docx" : "zip"))
            .contentType(MediaType.parseMediaType(docx
                ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document" : "application/zip"))
            .body(service.export(body));
    }
}
"""

HTTP_TEST = """package example;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.SpringBootConfiguration;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.autoconfigure.jdbc.DataSourceAutoConfiguration;
import org.springframework.boot.autoconfigure.mongo.MongoAutoConfiguration;
import org.springframework.boot.autoconfigure.data.redis.RedisAutoConfiguration;
import org.springframework.boot.autoconfigure.security.servlet.SecurityAutoConfiguration;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.boot.web.server.LocalServerPort;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.context.annotation.Import;
import org.springframework.http.*;
import org.springframework.test.context.junit4.SpringRunner;
import static org.junit.Assert.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.when;

@RunWith(SpringRunner.class)
@SpringBootTest(classes = HttpExportTest.TestApplication.class,
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = "spring.main.lazy-initialization=true")
public class HttpExportTest {
    @SpringBootConfiguration
    @EnableAutoConfiguration(exclude = {DataSourceAutoConfiguration.class,
        MongoAutoConfiguration.class, RedisAutoConfiguration.class, SecurityAutoConfiguration.class})
    @Import(ExportEndpoint.class)
    static class TestApplication {}

    @LocalServerPort private int port;
    @Autowired private TestRestTemplate client;
    @MockBean private ExportEndpoint.ExportService service;
    @MockBean private ExportEndpoint.SyncService sync;
    @MockBean private ExportEndpoint.SessionService session;

    @Test public void exportsOverHttp() {
        byte[] docx = {1, 2, 3, 4};
        byte[] zip = {5, 6, 7, 8};
        when(service.export(any())).thenReturn(docx, zip);
        ResponseEntity<byte[]> first = post("{\\\"id\\\":\\\"123\\\"}");
        assertEquals(200, first.getStatusCodeValue());
        assertArrayEquals(docx, first.getBody());
        assertTrue(first.getHeaders().getContentType().toString().contains("wordprocessingml.document"));
        assertTrue(first.getHeaders().getFirst(HttpHeaders.CONTENT_DISPOSITION).contains(".docx"));
        ResponseEntity<byte[]> second = post("{}");
        assertEquals(200, second.getStatusCodeValue());
        assertArrayEquals(zip, second.getBody());
        assertEquals("application/zip", second.getHeaders().getContentType().toString());
        assertTrue(second.getHeaders().getFirst(HttpHeaders.CONTENT_DISPOSITION).contains(".zip"));
    }

    private ResponseEntity<byte[]> post(String body) {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        return client.exchange("http://localhost:" + port + "/export", HttpMethod.POST,
            new HttpEntity<>(body, headers), byte[].class);
    }
}
"""

FRAMEWORK_CASES = """package example;
import java.lang.annotation.*;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.extension.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

public class FrameworkCases {
    public static class NoopExtension implements Extension {}
    @Retention(RetentionPolicy.RUNTIME) @Target(ElementType.TYPE)
    @ExtendWith(NoopExtension.class) @Tag("helper")
    public @interface Helpers {}
    @Helpers @DisplayName("still JUnit 4")
    public static class Legacy {
        @BeforeEach public void ignoredJupiterCallback() { throw new AssertionError("not a Jupiter test"); }
        @org.junit.Test public void runs() {}
    }

    @Retention(RetentionPolicy.RUNTIME) @Target(ElementType.METHOD)
    @Test public @interface ComposedTest {}
    public interface TestContract { @Test default void inheritedDefault() {} }
    public static class Base { @Test public void inherited() {} }
    public static class Modern extends Base implements TestContract {
        @ComposedTest public void composed() {}
        @RepeatedTest(2) public void repeated() {}
        @ParameterizedTest @ValueSource(ints = {1, 2}) public void parameterized(int n) {
            Assertions.assertTrue(n > 0);
        }
        @TestFactory public java.util.stream.Stream<DynamicTest> dynamic() {
            return java.util.stream.Stream.of(DynamicTest.dynamicTest("dynamic", () -> {}));
        }
    }
    @Helpers
    public static class NestedOnly {
        @Nested public class Child { @Test public void nested() {} }
    }
    @Helpers
    public static class NoTests {}
    public static class Conflicting {
        @org.junit.Test @org.junit.jupiter.api.Test public void genuinelyAmbiguous() {}
    }
}
"""


def make_project(root):
    root.mkdir()
    (root / "pom.xml").write_text(POM, encoding="utf-8")
    for name, source in (
        ("main/java/example/ExportEndpoint.java", ENDPOINT),
        ("test/java/example/HttpExportTest.java", HTTP_TEST),
        ("test/java/example/FrameworkCases.java", FRAMEWORK_CASES),
    ):
        path = root / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


@pytest.mark.mcp_java_e2e
@pytest.mark.parametrize("java_setting", ["JOLINK_TEST_JAVA8_HOME", "JAVA_HOME"])
def test_spring_http_and_framework_discovery_through_real_mcp(tmp_path, java_setting):
    require_real_mcp_java_e2e()
    java = os.environ.get(java_setting)
    if not java:
        pytest.skip(f"set {java_setting}")
    project = make_project(tmp_path / "project")
    env = {**os.environ, "JAVA_HOME": java,
           "PATH": str(Path(java) / "bin") + os.pathsep + os.environ.get("PATH", ""),
           "XDG_CACHE_HOME": str(tmp_path / "cache")}

    async def scenario():
        for reopened in (False, True):
            with temporary_stderr() as stderr:
                async with open_mcp_session(stderr, environment=env) as session:
                    async def run(selectors):
                        reply = await session.call_tool("java_fast_test", {
                            "project_path": str(project), "tests": selectors, "timeout": 30,
                        })
                        payload = dict(reply.structuredContent)
                        with anyio.fail_after(180):
                            while payload["status"] in {"starting", "bootstrapping", "compiling", "running"}:
                                await anyio.sleep(0.2)
                                payload = dict((await session.call_tool("java_fast_test", {
                                    "action": "result", "test_run_id": payload["test_run_id"],
                                })).structuredContent)
                        if not payload.get("passed"):
                            payload = dict((await session.call_tool("java_fast_test", {
                                "action": "result", "test_run_id": payload["test_run_id"],
                            })).structuredContent)
                        return payload

                    async def passed(selectors, count, framework):
                        result = await run(selectors)
                        assert result.get("passed") is True, result
                        assert result["tests"] == result["passed_count"] == count, result
                        assert result["framework"] == framework, result
                        return result

                    http = await passed(["example.HttpExportTest"], 1, "junit4")
                    if reopened:
                        assert http["compiled_source_count"] == 0, http
                        continue
                    await passed(["example.HttpExportTest#exportsOverHttp"], 1, "junit4")
                    await passed(["example.FrameworkCases$Legacy"], 1, "junit4")
                    await passed(["example.FrameworkCases$Modern"], 8, "junit5")
                    await passed(["example.FrameworkCases$Modern#composed"], 1, "junit5")
                    await passed(["example.FrameworkCases$Modern#parameterized"], 2, "junit5")
                    await passed(["example.FrameworkCases$Modern#inherited"], 1, "junit5")
                    await passed(["example.FrameworkCases$Modern#inheritedDefault"], 1, "junit5")
                    await passed(["example.FrameworkCases$NestedOnly"], 1, "junit5")
                    await passed(["example.FrameworkCases$NestedOnly$Child#nested"], 1, "junit5")
                    await passed(["example.HttpExportTest", "example.FrameworkCases$Modern"], 9, "mixed")
                    for name in ("NoTests", "Conflicting"):
                        failed = await run(["example.FrameworkCases$" + name])
                        assert failed["error_code"] == "TEST_RUNNER_FAILED", failed
                        assert "ambiguous or unsupported" in failed["message"], failed
                    # A runner failure must not prevent the next valid selection.
                    await passed(["example.HttpExportTest"], 1, "junit4")

    anyio.run(scenario)
