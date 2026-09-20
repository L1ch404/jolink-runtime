package io.jolink.maven;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import org.apache.maven.plugin.MojoExecutionException;
import org.apache.maven.project.MavenProject;

/** Locate tests in Maven's effective source roots before resolving dependencies. */
final class TestModuleSelection {
    static MavenProject select(List<MavenProject> projects, MavenProject requested, File directory,
            String selectors, String sourceFiles, Path output) throws IOException, MojoExecutionException {
        if (requested != null && "jar".equals(requested.getPackaging())) return requested;
        List<MavenProject> jars = new ArrayList<>();
        List<MavenProject> matches = new ArrayList<>();
        for (MavenProject project : projects) {
            if (!"jar".equals(project.getPackaging())) continue;
            jars.add(project);
            boolean all = true;
            for (String selector : selectors.split(",")) {
                String file = selector.split("#", 2)[0].split("\\$", 2)[0].replace('.', '/') + ".java";
                boolean found = false;
                for (String root : project.getTestCompileSourceRoots()) {
                    File source = new File(root);
                    if (!source.isAbsolute()) source = new File(project.getBasedir(), root);
                    found |= new File(source, file).isFile();
                }
                all &= found;
            }
            if (all) matches.add(project);
        }
        if (matches.size() == 1) return matches.get(0);
        if (matches.isEmpty() && jars.size() == 1) return jars.get(0);
        if (matches.isEmpty() && sourceFiles != null && !sourceFiles.isEmpty()) {
            for (MavenProject project : jars) {
                boolean all = true;
                for (String name : sourceFiles.split(",")) {
                    File file = new File(name);
                    if (!file.isAbsolute()) file = new File(directory, name);
                    all &= file.getCanonicalFile().toPath().startsWith(project.getBasedir().getCanonicalFile().toPath());
                }
                if (all) matches.add(project);
            }
            if (matches.size() == 1) return matches.get(0);
        }
        String code = jars.isEmpty() ? "FAST_TEST_PACKAGING_UNSUPPORTED" : "FAST_TEST_MODULE_AMBIGUOUS";
        StringBuilder error = new StringBuilder(code);
        for (MavenProject project : matches.isEmpty() ? jars : matches) {
            error.append('\n').append(project.getBasedir().getCanonicalPath());
        }
        Files.write(output.resolve("selection-error.txt"), error.toString().getBytes(StandardCharsets.UTF_8));
        throw new MojoExecutionException("Select test classes from one Maven jar module, or specify its project_path.");
    }
}
