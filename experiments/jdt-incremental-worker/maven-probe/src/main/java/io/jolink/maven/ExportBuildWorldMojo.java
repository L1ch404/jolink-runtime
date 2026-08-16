package io.jolink.maven;

import java.io.BufferedReader;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Collections;
import java.util.List;

import org.apache.maven.artifact.DependencyResolutionRequiredException;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.plugin.AbstractMojo;
import org.apache.maven.plugin.MojoExecutionException;
import org.apache.maven.plugins.annotations.LifecyclePhase;
import org.apache.maven.plugins.annotations.Mojo;
import org.apache.maven.plugins.annotations.Parameter;
import org.apache.maven.plugins.annotations.ResolutionScope;
import org.apache.maven.project.MavenProject;

/** Export facts already resolved by the active Maven session. */
@Mojo(
    name = "export-build-world",
    defaultPhase = LifecyclePhase.COMPILE,
    requiresDependencyResolution = ResolutionScope.COMPILE,
    requiresProject = true,
    threadSafe = true
)
public final class ExportBuildWorldMojo extends AbstractMojo {
    private static final String SCHEMA = "jolink.maven-build-world-probe.v1";
    private static final String PROBE_VERSION = "0.1.0-spike1";
    private static final String IMPLEMENTATION_ID_RESOURCE =
        "/META-INF/jolink/probe-implementation-id.txt";

    @Parameter(defaultValue = "${project}", readonly = true, required = true)
    private MavenProject project;

    @Parameter(defaultValue = "${session}", readonly = true, required = true)
    private MavenSession session;

    @Parameter(property = "jolink.probe.outputDirectory", required = true)
    private File outputDirectory;

    @Override
    public void execute() throws MojoExecutionException {
        if (outputDirectory == null) {
            throw new MojoExecutionException("jolink.probe.outputDirectory is required");
        }
        final List<String> classpath;
        try {
            classpath = project.getCompileClasspathElements();
        } catch (DependencyResolutionRequiredException error) {
            throw new MojoExecutionException("Compile classpath is unresolved", error);
        }
        String json = render(classpath);
        try {
            Path directory = outputDirectory.getCanonicalFile().toPath();
            Files.createDirectories(directory);
            String identity = value(project.getGroupId()) + ":"
                + value(project.getArtifactId()) + ":"
                + value(project.getVersion()) + ":"
                + canonical(project.getBasedir());
            Path destination = directory.resolve(sha256(identity) + ".json");
            Path temporary = directory.resolve("." + destination.getFileName() + ".tmp");
            Files.write(temporary, json.getBytes(StandardCharsets.UTF_8));
            try {
                Files.move(
                    temporary,
                    destination,
                    StandardCopyOption.ATOMIC_MOVE,
                    StandardCopyOption.REPLACE_EXISTING
                );
            } catch (AtomicMoveNotSupportedException ignored) {
                Files.move(
                    temporary,
                    destination,
                    StandardCopyOption.REPLACE_EXISTING
                );
            }
            getLog().info("joLink Build World facts exported for " + project.getId());
        } catch (IOException error) {
            throw new MojoExecutionException("Unable to write joLink Build World facts", error);
        }
    }

    private String render(List<String> classpath) throws MojoExecutionException {
        StringBuilder out = new StringBuilder(4096);
        out.append('{');
        field(out, "schema", SCHEMA, false);
        field(out, "probeVersion", PROBE_VERSION, true);
        field(out, "probeImplementationId", implementationId(), true);
        out.append(",\"project\":{");
        field(out, "groupId", project.getGroupId(), false);
        field(out, "artifactId", project.getArtifactId(), true);
        field(out, "version", project.getVersion(), true);
        field(out, "packaging", project.getPackaging(), true);
        field(out, "baseDirectory", canonical(project.getBasedir()), true);
        out.append('}');
        stringList(out, "requestedGoals", session.getGoals(), true);
        stringList(out, "compileSourceRoots", project.getCompileSourceRoots(), true);
        stringList(out, "compileClasspathElements", classpath, true);
        field(
            out,
            "outputDirectory",
            project.getBuild() == null ? null : canonical(new File(project.getBuild().getOutputDirectory())),
            true
        );
        out.append(",\"reactorProjects\":[");
        List<MavenProject> projects = session.getProjects();
        if (projects == null) {
            projects = Collections.emptyList();
        }
        boolean first = true;
        for (MavenProject item : projects) {
            if (!first) {
                out.append(',');
            }
            first = false;
            out.append('{');
            field(out, "groupId", item.getGroupId(), false);
            field(out, "artifactId", item.getArtifactId(), true);
            field(out, "version", item.getVersion(), true);
            field(out, "packaging", item.getPackaging(), true);
            field(out, "baseDirectory", canonical(item.getBasedir()), true);
            field(
                out,
                "outputDirectory",
                item.getBuild() == null ? null : canonical(new File(item.getBuild().getOutputDirectory())),
                true
            );
            out.append('}');
        }
        out.append(']');
        out.append('}').append('\n');
        return out.toString();
    }

    private static String implementationId() throws MojoExecutionException {
        try (InputStream stream = ExportBuildWorldMojo.class.getResourceAsStream(
            IMPLEMENTATION_ID_RESOURCE
        ); BufferedReader reader = stream == null ? null : new BufferedReader(
            new InputStreamReader(stream, StandardCharsets.US_ASCII)
        )) {
            if (stream == null) {
                throw new MojoExecutionException(
                    "The joLink Probe implementation identity is unavailable"
                );
            }
            String value = reader.readLine();
            if (value == null || reader.readLine() != null) {
                throw new MojoExecutionException(
                    "The joLink Probe implementation identity is invalid"
                );
            }
            value = value.trim();
            if (!value.matches("[0-9a-f]{64}")) {
                throw new MojoExecutionException(
                    "The joLink Probe implementation identity is invalid"
                );
            }
            return value;
        } catch (IOException error) {
            throw new MojoExecutionException(
                "Unable to read the joLink Probe implementation identity",
                error
            );
        }
    }

    private static void field(
        StringBuilder out, String name, String value, boolean comma
    ) {
        if (comma) {
            out.append(',');
        }
        quote(out, name);
        out.append(':');
        if (value == null) {
            out.append("null");
        } else {
            quote(out, value);
        }
    }

    private static void stringList(
        StringBuilder out, String name, List<String> values, boolean comma
    ) {
        if (comma) {
            out.append(',');
        }
        quote(out, name);
        out.append(':').append('[');
        boolean first = true;
        if (values != null) {
            for (String value : values) {
                if (!first) {
                    out.append(',');
                }
                first = false;
                quote(out, value);
            }
        }
        out.append(']');
    }

    private static void quote(StringBuilder out, String value) {
        out.append('"');
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            switch (character) {
                case '"': out.append("\\\""); break;
                case '\\': out.append("\\\\"); break;
                case '\b': out.append("\\b"); break;
                case '\f': out.append("\\f"); break;
                case '\n': out.append("\\n"); break;
                case '\r': out.append("\\r"); break;
                case '\t': out.append("\\t"); break;
                default:
                    if (character < 0x20) {
                        out.append(String.format("\\u%04x", (int) character));
                    } else {
                        out.append(character);
                    }
            }
        }
        out.append('"');
    }

    private static String canonical(File path) throws MojoExecutionException {
        if (path == null) {
            return null;
        }
        try {
            return path.getCanonicalPath();
        } catch (IOException error) {
            throw new MojoExecutionException("Unable to canonicalize Maven path", error);
        }
    }

    private static String value(String value) {
        return value == null ? "" : value;
    }

    private static String sha256(String value) throws MojoExecutionException {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] bytes = digest.digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder out = new StringBuilder(bytes.length * 2);
            for (byte item : bytes) {
                out.append(String.format("%02x", item & 0xff));
            }
            return out.toString();
        } catch (NoSuchAlgorithmException error) {
            throw new MojoExecutionException("SHA-256 is unavailable", error);
        }
    }
}
