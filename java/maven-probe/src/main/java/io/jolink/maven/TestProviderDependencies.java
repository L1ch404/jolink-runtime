package io.jolink.maven;

import java.io.File;
import java.util.*;
import java.util.jar.JarFile;
import org.apache.maven.RepositoryUtils;
import org.apache.maven.model.Plugin;
import org.apache.maven.project.MavenProject;
import org.eclipse.aether.RepositorySystem;
import org.eclipse.aether.RepositorySystemSession;
import org.eclipse.aether.artifact.Artifact;
import org.eclipse.aether.artifact.DefaultArtifact;
import org.eclipse.aether.collection.CollectRequest;
import org.eclipse.aether.graph.Dependency;
import org.eclipse.aether.resolution.ArtifactResult;
import org.eclipse.aether.resolution.ArtifactRequest;
import org.eclipse.aether.resolution.DependencyRequest;

/** Identify the provider and runtime libraries that a standalone Runner can actually use. */
final class TestProviderDependencies {
    final Set<String> extra = new LinkedHashSet<>();
    String framework;
    private boolean select(String name) {
        if (framework != null && !name.equals(framework)) return false;
        framework = name;
        return true;
    }
    private static String key(Artifact a) {
        return a.getGroupId() + ":" + a.getArtifactId() + ":" + a.getExtension() + ":" + a.getClassifier();
    }
    static TestProviderDependencies resolve(Plugin plugin, MavenProject project, RepositorySystem resolver,
            RepositorySystemSession session) throws Exception {
        TestProviderDependencies result = new TestProviderDependencies();
        Map<String,File> projectFiles = new HashMap<>();
        for (org.apache.maven.artifact.Artifact a : project.getArtifacts())
            if (a.getFile() != null) projectFiles.put(key(RepositoryUtils.toArtifact(a)), a.getFile().getCanonicalFile());
        Map<String,File> runtime = new HashMap<>(projectFiles);
        String platformVersion = null;
        for (org.apache.maven.model.Dependency d : plugin.getDependencies()) {
            Dependency dep = RepositoryUtils.toDependency(d, session.getArtifactTypeRegistry());
            Artifact root = dep.getArtifact();
            if ("org.apache.maven.surefire".equals(root.getGroupId())
                    && ("surefire-junit47".equals(root.getArtifactId()) || "surefire-junit4".equals(root.getArtifactId()))
                    && "jar".equals(root.getExtension()) && root.getClassifier().isEmpty()
                    && root.getVersion().equals(plugin.getVersion())) {
                // This dependency selects Surefire's JUnit 4 provider; its Maven implementation
                // libraries do not belong on the test application's classpath.
                if (!result.select("junit4")) return null;
                continue;
            }
            CollectRequest request = new CollectRequest().setRepositories(project.getRemotePluginRepositories());
            request.addDependency(dep.setScope("runtime"));
            boolean seenRoot = false, engine = false;
            List<String> missing = new ArrayList<>();
            for (ArtifactResult r : resolver.resolveDependencies(session, new DependencyRequest(request,
                    (node, parents) -> node.getDependency() == null ||
                            Arrays.asList("compile","runtime","").contains(node.getDependency().getScope())))
                    .getArtifactResults()) {
                Artifact artifact = r.getArtifact();
                String id = key(artifact);
                if ("org.junit.platform:junit-platform-engine:jar:".equals(id))
                    platformVersion = artifact.getVersion();
                File file = artifact.getFile().getCanonicalFile();
                File previous = runtime.put(id, file);
                if (previous != null && !previous.equals(file)) return null;
                if (!projectFiles.containsKey(id)) missing.add(file.getPath());
                if (id.equals(key(root))) {
                    if (!file.isFile() || !"jar".equals(artifact.getExtension())) return null;
                    seenRoot = true;
                    try (JarFile jar = new JarFile(file)) {
                        if (jar.getJarEntry("META-INF/services/org.apache.maven.surefire.api.provider.SurefireProvider") != null
                                || jar.getJarEntry("META-INF/services/org.apache.maven.surefire.providerapi.SurefireProvider") != null)
                            return null;
                        engine = jar.getJarEntry("META-INF/services/org.junit.platform.engine.TestEngine") != null;
                    }
                }
            }
            if (!seenRoot) return null;
            if (engine) {
                if (!result.select("junit5")) return null;
                result.extra.addAll(missing);
            } else if (!missing.isEmpty()) return null; // Not an engine; only an already-present closure is represented.
        }
        if ("junit5".equals(result.framework) && platformVersion != null
                && !projectFiles.containsKey("org.junit.platform:junit-platform-engine:jar:")
                && !runtime.containsKey("org.junit.platform:junit-platform-launcher:jar:")) {
            // The normal project launcher companion resolver cannot see an engine
            // that exists solely on the plugin side. Resolve its exact companion,
            // not whichever launcher happens to be present in the local cache.
            Artifact launcher = new DefaultArtifact("org.junit.platform", "junit-platform-launcher", "jar", platformVersion);
            result.extra.add(resolver.resolveArtifact(session, new ArtifactRequest(launcher,
                    project.getRemotePluginRepositories(), "plugin")).getArtifact().getFile().getCanonicalPath());
        }
        return result;
    }
}
