"""Shared real Gradle fixtures used by product validation scripts."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path


def run(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}):\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed


def build_local_processor(root: Path, java8_home: Path) -> Path:
    source = root / "processor-src/fixture/MarkerProcessor.java"
    classes = root / "processor-classes"
    source.parent.mkdir(parents=True)
    classes.mkdir()
    source.write_text(
        "package fixture; "
        "@javax.annotation.processing.SupportedAnnotationTypes(\"*\") "
        "@javax.annotation.processing.SupportedSourceVersion("
        "javax.lang.model.SourceVersion.RELEASE_8) "
        "public class MarkerProcessor extends javax.annotation.processing.AbstractProcessor { "
        "private boolean written; public boolean process("
        "java.util.Set<? extends javax.lang.model.element.TypeElement> annotations, "
        "javax.annotation.processing.RoundEnvironment round) { "
        "if (!written) { try { javax.tools.FileObject file = processingEnv.getFiler()"
        ".createResource(javax.tools.StandardLocation.CLASS_OUTPUT, \"\", "
        "\"META-INF/jolink-processor.txt\"); "
        "try (java.io.Writer out = file.openWriter()) { out.write(\"processed\"); } "
        "written = true; } catch (Exception error) { throw new RuntimeException(error); } } "
        "return false; } }\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        (
            str(java8_home / "bin/javac"),
            "-source",
            "8",
            "-target",
            "8",
            "-d",
            str(classes),
            str(source),
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout + completed.stderr)
    jar = root / "marker-processor.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        for class_file in sorted(classes.rglob("*.class")):
            archive.write(class_file, class_file.relative_to(classes).as_posix())
        archive.writestr(
            "META-INF/services/javax.annotation.processing.Processor",
            "fixture.MarkerProcessor\n",
        )
    return jar


def create_fixture(
    root: Path,
    *,
    kotlin_dsl: bool,
    processor_jar: Path | None = None,
    junit_jars: tuple[Path, ...] = (),
    custom_test_runtime: bool = True,
) -> Path:
    project = root / ("kotlin-dsl" if kotlin_dsl else "groovy-dsl")
    main = project / "src/main/java/example/TextService.java"
    test = project / "src/test/java/example/TextServiceTest.java"
    main.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    libraries = project / "libs"
    libraries.mkdir()
    for name, value in (("dependency-a.jar", "A"), ("dependency-b.jar", "B")):
        with zipfile.ZipFile(libraries / name, "w") as archive:
            archive.writestr("META-INF/jolink-order.txt", value)
    if processor_jar is None:
        with zipfile.ZipFile(libraries / "processor.jar", "w") as archive:
            archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n\n")
    else:
        shutil.copyfile(processor_jar, libraries / "processor.jar")
    junit_directory = libraries / "junit"
    junit_directory.mkdir()
    for jar in junit_jars:
        shutil.copyfile(jar, junit_directory / jar.name)
    (project / ("settings.gradle.kts" if kotlin_dsl else "settings.gradle")).write_text(
        'rootProject.name = "jolink-gradle-fixture"\n'
        if kotlin_dsl
        else "rootProject.name = 'jolink-gradle-fixture'\n",
        encoding="utf-8",
    )
    (project / "gradle.properties").write_text(
        "org.gradle.configuration-cache=true\n",
        encoding="utf-8",
    )
    if kotlin_dsl:
        build = """plugins { java }

java {
    sourceCompatibility = JavaVersion.VERSION_11
    targetCompatibility = JavaVersion.VERSION_11
    toolchain { languageVersion.set(JavaLanguageVersion.of(11)) }
}

dependencies {
    implementation(files("libs/dependency-a.jar", "libs/dependency-b.jar"))
    annotationProcessor(files("libs/processor.jar"))
    testAnnotationProcessor(files("libs/processor.jar"))
    testImplementation(fileTree("libs/junit") { include("*.jar") })
}

tasks.withType<JavaCompile>().configureEach {
    options.encoding = "UTF-8"
}

tasks.test {
    useJUnitPlatform()
%s
}
""" % (
            """    workingDir = layout.buildDirectory.dir("test-working").get().asFile
    systemProperty("jolink.fixture.token", "sensitive-system-value")
    environment("JOLINK_FIXTURE_ENV", "sensitive-environment-value")
    jvmArgs("-Xmx128m")"""
            if custom_test_runtime
            else ""
        )
    else:
        build = """plugins { id 'java' }

java {
    sourceCompatibility = JavaVersion.VERSION_11
    targetCompatibility = JavaVersion.VERSION_11
    toolchain { languageVersion = JavaLanguageVersion.of(11) }
}

dependencies {
    implementation files('libs/dependency-a.jar', 'libs/dependency-b.jar')
    annotationProcessor files('libs/processor.jar')
    testAnnotationProcessor files('libs/processor.jar')
    testImplementation fileTree(dir: 'libs/junit', include: ['*.jar'])
}

tasks.withType(JavaCompile).configureEach {
    options.encoding = 'UTF-8'
}

test {
    useJUnitPlatform()
%s
}
""" % (
            """    workingDir = layout.buildDirectory.dir('test-working').get().asFile
    systemProperty 'jolink.fixture.token', 'sensitive-system-value'
    environment 'JOLINK_FIXTURE_ENV', 'sensitive-environment-value'
    jvmArgs '-Xmx128m'"""
            if custom_test_runtime
            else ""
        )
    (project / ("build.gradle.kts" if kotlin_dsl else "build.gradle")).write_text(
        build, encoding="utf-8"
    )
    main.write_text(
        "package example; public class TextService { "
        "public String strip(String value) { return value.strip(); } "
        "public static void main(String[] args) throws Exception { "
        "try (java.io.InputStream in = TextService.class.getClassLoader()"
        ".getResourceAsStream(\"META-INF/jolink-order.txt\")) { "
        "System.out.print((char) in.read()); } } }\n",
        encoding="utf-8",
    )
    test.write_text(
        (
            "package example; import org.junit.jupiter.api.Test; "
            "import static org.junit.jupiter.api.Assertions.assertEquals; "
            "public class TextServiceTest { @Test public void works() { "
            "assertEquals(\"x\", new TextService().strip(\" x \")); } }\n"
            if junit_jars
            else "package example; public class TextServiceTest { "
            "public void placeholder() { new TextService().strip(\" x \" ); } }\n"
        ),
        encoding="utf-8",
    )
    return project


def create_wrapper(
    project: Path,
    *,
    gradle: Path,
    version: str,
    distribution_zip: Path,
    environment: dict[str, str],
) -> None:
    run(
        (
            str(gradle),
            "--offline",
            "--no-daemon",
            "wrapper",
            "--gradle-version",
            version,
            "--distribution-type",
            "bin",
        ),
        cwd=project,
        environment=environment,
    )
    wrapper = project / "gradlew"
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    properties = project / "gradle/wrapper/gradle-wrapper.properties"
    rendered = properties.read_text(encoding="utf-8")
    rendered = "\n".join(
        (
            "distributionUrl=" + distribution_zip.as_uri()
            if line.startswith("distributionUrl=")
            else line
        )
        for line in rendered.splitlines()
    ) + "\n"
    properties.write_text(rendered, encoding="utf-8")


def private_distribution_zip(
    root: Path,
    *,
    gradle: Path,
    version: str,
) -> Path:
    distribution = gradle.parent.parent.resolve(strict=True)
    output = root / "private-distributions" / f"gradle-{version}-bin.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_file():
        return output
    archive = shutil.make_archive(
        str(output.with_suffix("")),
        "zip",
        root_dir=distribution.parent,
        base_dir=distribution.name,
    )
    return Path(archive).resolve(strict=True)

