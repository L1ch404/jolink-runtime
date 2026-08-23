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
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Properties;
import java.util.Set;
import java.util.jar.JarEntry;
import java.util.jar.JarFile;

import org.apache.maven.artifact.DependencyResolutionRequiredException;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.plugin.AbstractMojo;
import org.apache.maven.plugin.MojoExecutionException;
import org.apache.maven.plugins.annotations.LifecyclePhase;
import org.apache.maven.plugins.annotations.Mojo;
import org.apache.maven.plugins.annotations.Parameter;
import org.apache.maven.plugins.annotations.ResolutionScope;
import org.apache.maven.model.Plugin;
import org.apache.maven.model.PluginExecution;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

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
    private static final String PROBE_VERSION = "0.1.0-spike6";
    private static final String IMPLEMENTATION_ID_RESOURCE =
        "/META-INF/jolink/probe-implementation-id.txt";
    private static final String PROCESSOR_SERVICE =
        "META-INF/services/javax.annotation.processing.Processor";
    private static final long MAX_PROCESSOR_SERVICE_BYTES = 64L * 1024L;

    private static final class ProcessorFacts {
        final String processingMode;
        final String discoveryMode;
        final boolean compileClasspathDiscovery;
        final List<String> providerArtifactPaths;
        final List<String> providers;
        final List<String> options;
        final List<String> explicitProcessorNames;
        final int explicitPathDeclarationCount;
        final boolean executionProcessorConfigurationDetected;
        final boolean legacyProcessorOptionsDetected;
        final int legacyProcessorOptionCount;
        final boolean procPropertyDetected;
        final int procPropertySourceCount;
        final boolean unmodeledProcessorCompilerArgsDetected;
        final int unmodeledProcessorCompilerArgCount;

        ProcessorFacts(
            String processingMode,
            String discoveryMode,
            boolean compileClasspathDiscovery,
            List<String> providerArtifactPaths,
            List<String> providers,
            List<String> options,
            List<String> explicitProcessorNames,
            int explicitPathDeclarationCount,
            boolean executionProcessorConfigurationDetected,
            boolean legacyProcessorOptionsDetected,
            int legacyProcessorOptionCount,
            boolean procPropertyDetected,
            int procPropertySourceCount,
            boolean unmodeledProcessorCompilerArgsDetected,
            int unmodeledProcessorCompilerArgCount
        ) {
            this.processingMode = processingMode;
            this.discoveryMode = discoveryMode;
            this.compileClasspathDiscovery = compileClasspathDiscovery;
            this.providerArtifactPaths = providerArtifactPaths;
            this.providers = providers;
            this.options = options;
            this.explicitProcessorNames = explicitProcessorNames;
            this.explicitPathDeclarationCount = explicitPathDeclarationCount;
            this.executionProcessorConfigurationDetected =
                executionProcessorConfigurationDetected;
            this.legacyProcessorOptionsDetected = legacyProcessorOptionsDetected;
            this.legacyProcessorOptionCount = legacyProcessorOptionCount;
            this.procPropertyDetected = procPropertyDetected;
            this.procPropertySourceCount = procPropertySourceCount;
            this.unmodeledProcessorCompilerArgsDetected =
                unmodeledProcessorCompilerArgsDetected;
            this.unmodeledProcessorCompilerArgCount =
                unmodeledProcessorCompilerArgCount;
        }
    }

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
        String json = render(classpath, processorFacts(classpath));
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

    private String render(
        List<String> classpath,
        ProcessorFacts processors
    ) throws MojoExecutionException {
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
        out.append(",\"annotationProcessing\":{");
        field(out, "processingMode", processors.processingMode, false);
        field(out, "discoveryMode", processors.discoveryMode, true);
        booleanField(
            out,
            "compileClasspathDiscovery",
            processors.compileClasspathDiscovery,
            true
        );
        booleanField(
            out,
            "legacyProcessorOptionsDetected",
            processors.legacyProcessorOptionsDetected,
            true
        );
        numberField(
            out,
            "legacyProcessorOptionCount",
            processors.legacyProcessorOptionCount,
            true
        );
        booleanField(
            out,
            "procPropertyDetected",
            processors.procPropertyDetected,
            true
        );
        numberField(
            out,
            "procPropertySourceCount",
            processors.procPropertySourceCount,
            true
        );
        booleanField(
            out,
            "unmodeledProcessorCompilerArgsDetected",
            processors.unmodeledProcessorCompilerArgsDetected,
            true
        );
        numberField(
            out,
            "unmodeledProcessorCompilerArgCount",
            processors.unmodeledProcessorCompilerArgCount,
            true
        );
        stringList(
            out,
            "processorProviderArtifactPaths",
            processors.providerArtifactPaths,
            true
        );
        stringList(out, "providers", processors.providers, true);
        stringList(out, "options", processors.options, true);
        stringList(
            out,
            "explicitProcessorNames",
            processors.explicitProcessorNames,
            true
        );
        numberField(
            out,
            "explicitPathDeclarationCount",
            processors.explicitPathDeclarationCount,
            true
        );
        booleanField(
            out,
            "executionProcessorConfigurationDetected",
            processors.executionProcessorConfigurationDetected,
            true
        );
        out.append('}');
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

    private ProcessorFacts processorFacts(List<String> classpath)
        throws MojoExecutionException {
        Plugin plugin = compilerPlugin();
        Xpp3Dom configuration = pluginConfiguration(plugin);
        String proc = childValue(configuration, "proc");
        String processingMode = processingMode(proc);
        List<String> options = compilerOptions(configuration);
        List<String> explicitNames = childValues(
            child(configuration, "annotationProcessors"),
            "annotationProcessor"
        );
        Xpp3Dom explicitPaths = child(configuration, "annotationProcessorPaths");
        int explicitPathCount = explicitPaths == null
            ? 0 : explicitPaths.getChildCount();
        int legacyOptionCount = legacyProcessorOptionCount(configuration);
        int procPropertyCount = procPropertySourceCount();
        int unmodeledCompilerArgCount =
            unmodeledProcessorCompilerArgCount(configuration);
        boolean executionConfiguration =
            hasExecutionProcessorConfiguration(plugin);
        if (procPropertyCount > 0) {
            return new ProcessorFacts(
                processingMode,
                "PROC_PROPERTY_UNRESOLVED",
                false,
                Collections.<String>emptyList(),
                Collections.<String>emptyList(),
                options,
                explicitNames,
                explicitPathCount,
                executionConfiguration,
                legacyOptionCount > 0,
                legacyOptionCount,
                true,
                procPropertyCount,
                unmodeledCompilerArgCount > 0,
                unmodeledCompilerArgCount
            );
        }
        if (unmodeledCompilerArgCount > 0) {
            return new ProcessorFacts(
                processingMode,
                "COMPILER_ARGS_UNRESOLVED",
                false,
                Collections.<String>emptyList(),
                Collections.<String>emptyList(),
                options,
                explicitNames,
                explicitPathCount,
                executionConfiguration,
                legacyOptionCount > 0,
                legacyOptionCount,
                false,
                0,
                true,
                unmodeledCompilerArgCount
            );
        }
        if (executionConfiguration) {
            return new ProcessorFacts(
                processingMode,
                "EXECUTION_CONFIG_UNRESOLVED",
                false,
                Collections.<String>emptyList(),
                Collections.<String>emptyList(),
                options,
                explicitNames,
                explicitPathCount,
                true,
                legacyOptionCount > 0,
                legacyOptionCount,
                false,
                0,
                false,
                0
            );
        }
        if ("none".equalsIgnoreCase(proc)) {
            return new ProcessorFacts(
                "NONE",
                "DISABLED",
                false,
                Collections.<String>emptyList(),
                Collections.<String>emptyList(),
                options,
                explicitNames,
                explicitPathCount,
                false,
                legacyOptionCount > 0,
                legacyOptionCount,
                false,
                0,
                false,
                0
            );
        }
        if (explicitPathCount > 0) {
            return new ProcessorFacts(
                processingMode,
                "EXPLICIT_DECLARED_UNRESOLVED",
                false,
                Collections.<String>emptyList(),
                Collections.<String>emptyList(),
                options,
                explicitNames,
                explicitPathCount,
                false,
                legacyOptionCount > 0,
                legacyOptionCount,
                false,
                0,
                false,
                0
            );
        }

        List<String> artifacts = new ArrayList<String>();
        Set<String> providers = new LinkedHashSet<String>();
        for (String raw : classpath) {
            File entry = new File(raw);
            List<String> discovered = processorProviders(entry);
            if (!discovered.isEmpty()) {
                artifacts.add(canonical(entry));
                providers.addAll(discovered);
            }
        }
        List<String> sortedProviders = new ArrayList<String>(providers);
        Collections.sort(sortedProviders);
        return new ProcessorFacts(
            processingMode,
            "IMPLICIT_COMPILE_CLASSPATH",
            true,
            artifacts,
            sortedProviders,
            options,
            explicitNames,
            0,
            false,
            legacyOptionCount > 0,
            legacyOptionCount,
            false,
            0,
            false,
            0
        );
    }

    private Plugin compilerPlugin() {
        List<Plugin> plugins = project.getBuildPlugins();
        if (plugins == null) {
            return null;
        }
        for (Plugin plugin : plugins) {
            if ("maven-compiler-plugin".equals(plugin.getArtifactId())
                && (plugin.getGroupId() == null
                    || "org.apache.maven.plugins".equals(plugin.getGroupId()))) {
                return plugin;
            }
        }
        return null;
    }

    private static Xpp3Dom pluginConfiguration(Plugin plugin) {
        if (plugin == null) {
            return null;
        }
        Object configuration = plugin.getConfiguration();
        return configuration instanceof Xpp3Dom
            ? (Xpp3Dom) configuration : null;
    }

    private static boolean hasExecutionProcessorConfiguration(Plugin plugin) {
        if (plugin == null || plugin.getExecutions() == null) {
            return false;
        }
        for (PluginExecution execution : plugin.getExecutions()) {
            Object raw = execution.getConfiguration();
            if (raw instanceof Xpp3Dom
                && hasProcessorConfiguration((Xpp3Dom) raw)) {
                return true;
            }
        }
        return false;
    }

    private static boolean hasProcessorConfiguration(Xpp3Dom configuration) {
        if (configuration == null) {
            return false;
        }
        if (
            child(configuration, "annotationProcessorPaths") != null
            || child(configuration, "annotationProcessors") != null
            || !childValue(configuration, "proc").isEmpty()
            || !compilerOptions(configuration).isEmpty()
            || unmodeledProcessorCompilerArgCount(configuration) > 0
        ) {
            return true;
        }
        return legacyProcessorOptionCount(configuration) > 0;
    }

    private static int legacyProcessorOptionCount(Xpp3Dom configuration) {
        Xpp3Dom legacy = child(configuration, "compilerArguments");
        if (legacy == null) {
            return 0;
        }
        int count = 0;
        for (Xpp3Dom item : legacy.getChildren()) {
            if (item.getName().startsWith("A")) {
                count++;
            }
        }
        return count;
    }

    private int procPropertySourceCount() {
        int count = 0;
        if (hasProperty(project.getProperties(), "maven.compiler.proc")) {
            count++;
        }
        if (hasProperty(session.getUserProperties(), "maven.compiler.proc")) {
            count++;
        }
        if (hasProperty(session.getSystemProperties(), "maven.compiler.proc")) {
            count++;
        }
        return count;
    }

    private static boolean hasProperty(Properties values, String name) {
        return values != null && values.containsKey(name);
    }

    private static int unmodeledProcessorCompilerArgCount(
        Xpp3Dom configuration
    ) {
        int count = 0;
        Xpp3Dom arguments = child(configuration, "compilerArgs");
        if (arguments != null) {
            for (Xpp3Dom item : arguments.getChildren("arg")) {
                if (isUnmodeledProcessorCompilerArg(value(item))) {
                    count++;
                }
            }
        }
        String argument = childValue(configuration, "compilerArgument");
        if (isUnmodeledProcessorCompilerArg(argument)) {
            count++;
        }
        Xpp3Dom legacy = child(configuration, "compilerArguments");
        if (legacy != null) {
            for (Xpp3Dom item : legacy.getChildren()) {
                String name = item.getName().toLowerCase(Locale.ROOT);
                if (
                    "proc".equals(name)
                    || "processor".equals(name)
                    || "processorpath".equals(name)
                    || "processor-path".equals(name)
                    || "processor-module-path".equals(name)
                    || "s".equals(name)
                ) {
                    count++;
                }
            }
        }
        return count;
    }

    private static boolean isUnmodeledProcessorCompilerArg(String value) {
        String normalized = value == null
            ? "" : value.trim().toLowerCase(Locale.ROOT);
        for (String token : normalized.split("\\s+")) {
            if (
                token.startsWith("-proc:")
                || isOption(token, "-processor")
                || isOption(token, "-processorpath")
                || isOption(token, "--processor-path")
                || isOption(token, "--processor-module-path")
                || isOption(token, "-s")
            ) {
                return true;
            }
        }
        return false;
    }

    private static boolean isOption(String value, String option) {
        return value.equals(option)
            || value.startsWith(option + "=")
            || value.startsWith(option + " ");
    }

    private static String processingMode(String proc) {
        if (proc == null || proc.isEmpty() || "full".equalsIgnoreCase(proc)) {
            return "DEFAULT";
        }
        if ("none".equalsIgnoreCase(proc)) {
            return "NONE";
        }
        if ("only".equalsIgnoreCase(proc)) {
            return "ONLY";
        }
        return "UNKNOWN";
    }

    private static List<String> processorProviders(File entry)
        throws MojoExecutionException {
        try {
            if (entry.isDirectory()) {
                Path service = entry.toPath().resolve(PROCESSOR_SERVICE);
                if (!Files.isRegularFile(service)) {
                    return Collections.emptyList();
                }
                if (Files.size(service) > MAX_PROCESSOR_SERVICE_BYTES) {
                    throw new MojoExecutionException(
                        "Annotation Processor service declaration is too large"
                    );
                }
                return parseProcessorService(Files.readAllBytes(service));
            }
            if (!entry.isFile()) {
                return Collections.emptyList();
            }
            try (JarFile jar = new JarFile(entry)) {
                JarEntry service = jar.getJarEntry(PROCESSOR_SERVICE);
                if (service == null) {
                    return Collections.emptyList();
                }
                if (service.getSize() > MAX_PROCESSOR_SERVICE_BYTES) {
                    throw new MojoExecutionException(
                        "Annotation Processor service declaration is too large"
                    );
                }
                try (InputStream input = jar.getInputStream(service)) {
                    return parseProcessorService(readBounded(input));
                }
            }
        } catch (IOException error) {
            throw new MojoExecutionException(
                "Unable to inspect Annotation Processor service metadata",
                error
            );
        }
    }

    private static byte[] readBounded(InputStream input) throws IOException {
        java.io.ByteArrayOutputStream output = new java.io.ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        int total = 0;
        for (int count; (count = input.read(buffer)) >= 0;) {
            total += count;
            if (total > MAX_PROCESSOR_SERVICE_BYTES) {
                throw new IOException("Processor service declaration is too large");
            }
            output.write(buffer, 0, count);
        }
        return output.toByteArray();
    }

    private static List<String> parseProcessorService(byte[] data) {
        String text = new String(data, StandardCharsets.UTF_8);
        Set<String> values = new LinkedHashSet<String>();
        for (String line : text.split("\\r?\\n")) {
            String value = line.split("#", 2)[0].trim();
            if (!value.isEmpty()) {
                values.add(value);
            }
        }
        return new ArrayList<String>(values);
    }

    private static List<String> compilerOptions(Xpp3Dom configuration) {
        List<String> options = new ArrayList<String>();
        Xpp3Dom arguments = child(configuration, "compilerArgs");
        if (arguments != null) {
            for (Xpp3Dom item : arguments.getChildren("arg")) {
                String value = value(item);
                if (value.startsWith("-A")) {
                    options.add(value);
                }
            }
        }
        String argument = childValue(configuration, "compilerArgument");
        if (argument.startsWith("-A")) {
            options.add(argument);
        }
        Collections.sort(options);
        return options;
    }

    private static Xpp3Dom child(Xpp3Dom parent, String name) {
        return parent == null ? null : parent.getChild(name);
    }

    private static String childValue(Xpp3Dom parent, String name) {
        return value(child(parent, name));
    }

    private static List<String> childValues(Xpp3Dom parent, String name) {
        if (parent == null) {
            return Collections.emptyList();
        }
        List<String> values = new ArrayList<String>();
        for (Xpp3Dom item : parent.getChildren(name)) {
            String value = value(item);
            if (!value.isEmpty()) {
                values.add(value);
            }
        }
        Collections.sort(values);
        return values;
    }

    private static String value(Xpp3Dom value) {
        return value == null || value.getValue() == null
            ? "" : value.getValue().trim();
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

    private static void booleanField(
        StringBuilder out, String name, boolean value, boolean comma
    ) {
        if (comma) {
            out.append(',');
        }
        quote(out, name);
        out.append(':').append(value);
    }

    private static void numberField(
        StringBuilder out, String name, int value, boolean comma
    ) {
        if (comma) {
            out.append(',');
        }
        quote(out, name);
        out.append(':').append(value);
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
