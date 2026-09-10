package io.jolink.maven;

import java.io.File;
import java.io.IOException;
import java.util.*;
import org.apache.maven.RepositoryUtils;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.model.io.xpp3.MavenXpp3Writer;
import org.apache.maven.plugin.AbstractMojo;
import org.apache.maven.plugin.MojoExecutionException;
import org.apache.maven.plugins.annotations.*;
import org.apache.maven.project.*;
import org.apache.maven.repository.RepositorySystem;
import org.eclipse.aether.DefaultRepositorySystemSession;
import org.eclipse.aether.artifact.Artifact;
import org.eclipse.aether.graph.Dependency;
import org.eclipse.aether.repository.*;

/** Resolve Maven dependencies against local module outputs without a compile lifecycle. */
@Mojo(name="export-reactor-world", aggregator=true, requiresProject=true, threadSafe=true)
public final class ExportReactorWorldMojo extends AbstractMojo {
    @Parameter(defaultValue="${session}", readonly=true) private MavenSession session;
    @Parameter(property="jolink.probe.outputDirectory", required=true) private File output;
    @Parameter(property="jolink.probe.targetDirectory", required=true) private File target;
    @Parameter(property="jolink.probe.scope", defaultValue="test") private String scope;
    @Component private ProjectDependenciesResolver resolver;
    @Component private RepositorySystem repositories;
    @Component private org.eclipse.aether.RepositorySystem artifactResolver;

    public void execute() throws MojoExecutionException {
        try {
            Map<String, MavenProject> projects = new LinkedHashMap<>();
            MavenProject selected = null;
            for (MavenProject p : session.getProjects()) {
                projects.put(p.getGroupId()+":"+p.getArtifactId()+":"+p.getVersion(), p);
                if (p.getBasedir().getCanonicalFile().equals(target.getCanonicalFile())) selected = p;
            }
            if (selected == null) throw new MojoExecutionException("Target module is not in this reactor");
            output.mkdirs();
            final WorkspaceReader previous = session.getRepositorySession().getWorkspaceReader();
            DefaultRepositorySystemSession repositorySession = new DefaultRepositorySystemSession(session.getRepositorySession());
            repositorySession.setWorkspaceReader(new WorkspaceReader() {
                public WorkspaceRepository getRepository() { return new WorkspaceRepository("jolink-reactor"); }
                public File findArtifact(Artifact a) {
                    MavenProject p = projects.get(a.getGroupId()+":"+a.getArtifactId()+":"+a.getVersion());
                    if (p != null) {
                        if ("tests".equals(a.getClassifier()) && "jar".equals(a.getExtension())) return new File(p.getBuild().getTestOutputDirectory());
                        if (!a.getClassifier().isEmpty()) return previous == null ? null : previous.findArtifact(a);
                        if (a.getExtension().equals("pom")) return p.getFile();
                        if (a.getExtension().equals("jar")) return new File(p.getBuild().getOutputDirectory());
                    }
                    return previous == null ? null : previous.findArtifact(a);
                }
                public List<String> findVersions(Artifact a) {
                    List<String> versions = new ArrayList<>();
                    for (MavenProject p : projects.values())
                        if (p.getGroupId().equals(a.getGroupId()) && p.getArtifactId().equals(a.getArtifactId())) versions.add(p.getVersion());
                    if (previous != null) versions.addAll(previous.findVersions(a));
                    return versions;
                }
            });
            Deque<MavenProject> pending = new ArrayDeque<>();
            pending.add(selected);
            Set<String> visited = new HashSet<>();
            Set<String> testsRequired = new HashSet<>();
            if ("test".equals(scope)) testsRequired.add(selected.getId());
            while (!pending.isEmpty()) {
                MavenProject p = pending.removeFirst();
                final boolean tests = testsRequired.contains(p.getId());
                if (!visited.add(p.getId()+":"+tests)) continue;
                DefaultDependencyResolutionRequest request = new DefaultDependencyResolutionRequest(p, repositorySession);
                request.setResolutionFilter((node, parents) -> node.getDependency() == null
                        || tests || !"test".equals(node.getDependency().getScope()));
                DependencyResolutionResult resolved = resolver.resolve(request);
                Set<org.apache.maven.artifact.Artifact> artifacts = new LinkedHashSet<>();
                for (Dependency dependency : resolved.getDependencies()) {
                    if (!tests && "test".equals(dependency.getScope())) continue;
                    Artifact a = dependency.getArtifact();
                    org.apache.maven.artifact.Artifact legacy = RepositoryUtils.toArtifact(a);
                    legacy.setScope(dependency.getScope());
                    artifacts.add(legacy);
                    MavenProject local = projects.get(a.getGroupId()+":"+a.getArtifactId()+":"+a.getVersion());
                    if (local != null && (a.getClassifier().isEmpty() || "tests".equals(a.getClassifier())) && !"pom".equals(local.getPackaging())) {
                        if ("tests".equals(a.getClassifier())) testsRequired.add(local.getId());
                        pending.add(local);
                    }
                }
                p.setArtifacts(artifacts);
                p.getProperties().setProperty("jolink.probe.testSourcesRequired", Boolean.toString(tests));
                new ExportBuildWorldMojo().exportProject(p, session, repositories, artifactResolver, repositorySession, output);
                String key = Integer.toHexString(p.getBasedir().getCanonicalPath().hashCode());
                try (java.io.Writer writer = java.nio.file.Files.newBufferedWriter(
                        output.toPath().resolve(key+".pom.xml"), java.nio.charset.StandardCharsets.UTF_8)) {
                    new MavenXpp3Writer().write(writer, p.getModel());
                }
            }
        } catch (DependencyResolutionException | IOException error) {
            throw new MojoExecutionException("Unable to resolve reactor Build World", error);
        }
    }
}
