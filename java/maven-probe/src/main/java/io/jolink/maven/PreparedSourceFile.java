package io.jolink.maven;

import java.io.File;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.plugin.BuildPluginManager;
import org.apache.maven.plugin.MojoExecution;
import org.apache.maven.plugin.PluginParameterExpressionEvaluator;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/** Run a named Java source generator without writing its result into the developer's source root. */
final class PreparedSourceFile {
    static final String KEY = "jolink.source.overrides";

    static void execute(MavenSession session, MavenProject project, MojoExecution mojo,
                        BuildPluginManager plugins, Map<String, String> overrides) throws Exception {
        Xpp3Dom original = mojo.getConfiguration();
        Xpp3Dom output = original.getChild("outputFile");
        if ("com.google.code.maven-replacer-plugin".equals(mojo.getGroupId())
                && "replacer".equals(mojo.getArtifactId()) && "replace".equals(mojo.getGoal())) {
            ReplacerSourceFiles.execute(session, project, mojo, plugins, overrides);
            return;
        }
        PluginParameterExpressionEvaluator evaluator = new PluginParameterExpressionEvaluator(session, mojo);
        String raw = output == null ? null : output.getValue() != null
                ? output.getValue() : output.getAttribute("default-value");
        Object resolved = raw == null ? null : evaluator.evaluate(raw);
        if (resolved == null) { plugins.executeMojo(session, mojo); return; }
        Path file = evaluator.alignToBaseDirectory(new File(resolved.toString())).toPath().toAbsolutePath().normalize();
        run(session, project, mojo, plugins, original, file, overrides);
    }

    /** Identify both a named source symlink and an aliased output pointing back into source. */
    static Path sourceKey(MavenProject project, Path file) throws Exception {
        if (!file.toString().endsWith(".java")) return null;
        Path logicalBuild = new File(project.getBuild().getDirectory()).toPath().toAbsolutePath().normalize();
        Path build = new File(project.getBuild().getDirectory()).getCanonicalFile().toPath();
        Path real = file.toFile().getCanonicalFile().toPath();
        List<String> roots = new ArrayList<>(project.getCompileSourceRoots());
        roots.addAll(project.getTestCompileSourceRoots());
        for (String name : roots) {
            Path root = new File(name).toPath().toAbsolutePath().normalize();
            Path actual = root.toFile().getCanonicalFile().toPath();
            if (file.startsWith(root) && !file.startsWith(logicalBuild)) return actual.resolve(root.relativize(file));
            if (real.startsWith(actual) && !real.startsWith(build)) return real;
        }
        return null;
    }

    static void run(MavenSession session, MavenProject project, MojoExecution mojo, BuildPluginManager plugins,
                    Xpp3Dom input, Path file, Map<String,String> overrides) throws Exception {
        Xpp3Dom original = mojo.getConfiguration();
        Path key = sourceKey(project, file);
        if (key == null) {
            try { mojo.setConfiguration(input); plugins.executeMojo(session, mojo); }
            finally { mojo.setConfiguration(original); }
            return;
        }
        Path cache = Paths.get(System.getProperty("user.home"), ".cache", "jolink-runtime", "prepared-java");
        Files.createDirectories(cache);
        Path temp = Files.createTempDirectory(cache, "prepare-"), target = temp.resolve(file.getFileName());
        try {
            Xpp3Dom privateConfig = new Xpp3Dom(input);
            Xpp3Dom out = privateConfig.getChild("outputFile");
            if (out == null) { out = new Xpp3Dom("outputFile"); privateConfig.addChild(out); }
            out.setValue(target.toString());
            mojo.setConfiguration(privateConfig);
            plugins.executeMojo(session, mojo);
            if (Files.isRegularFile(target)) {
                StringBuilder digest = new StringBuilder();
                for (byte b : MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(target)))
                    digest.append(String.format("%02x", b));
                Path copy = cache.resolve(digest.toString()).resolve(file.getFileName());
                Files.createDirectories(copy.getParent());
                try { Files.move(target, copy); } catch (FileAlreadyExistsException identical) { /* same content */ }
                overrides.put(key.toString(), copy.toString());
            }
        } finally {
            mojo.setConfiguration(original);
            Files.deleteIfExists(target);
            Files.deleteIfExists(temp);
        }
    }

}
