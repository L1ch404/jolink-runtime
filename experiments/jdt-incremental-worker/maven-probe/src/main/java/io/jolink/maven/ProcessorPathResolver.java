package io.jolink.maven;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import org.apache.maven.RepositoryUtils;
import org.apache.maven.model.Dependency;
import org.apache.maven.model.Exclusion;
import org.apache.maven.plugin.MojoExecutionException;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;
import org.eclipse.aether.RepositorySystem;
import org.eclipse.aether.RepositorySystemSession;
import org.eclipse.aether.collection.CollectRequest;
import org.eclipse.aether.resolution.ArtifactResult;
import org.eclipse.aether.resolution.DependencyRequest;
import org.eclipse.aether.resolution.DependencyResolutionException;
import org.eclipse.aether.util.filter.DependencyFilterUtils;

/** Resolve a processor classpath using Maven's active repositories/session. */
final class ProcessorPathResolver {
    static List<String> resolve(MavenProject project, RepositorySystem resolver,
            RepositorySystemSession session, Xpp3Dom paths, boolean manageTransitives)
            throws MojoExecutionException {
        CollectRequest request = new CollectRequest();
        request.setRepositories(project.getRemoteProjectRepositories());
        for (Xpp3Dom path : paths.getChildren()) {
            Dependency dependency = new Dependency();
            dependency.setGroupId(value(path, "groupId"));
            dependency.setArtifactId(value(path, "artifactId"));
            dependency.setVersion(value(path, "version"));
            dependency.setScope("runtime");
            if (!value(path, "type").isEmpty()) dependency.setType(value(path, "type"));
            if (!value(path, "classifier").isEmpty()) dependency.setClassifier(value(path, "classifier"));
            if (dependency.getVersion().isEmpty() && project.getDependencyManagement() != null) {
                for (Dependency managed : project.getDependencyManagement().getDependencies()) {
                    if (managed.getManagementKey().equals(dependency.getManagementKey())) {
                        dependency.setVersion(managed.getVersion());
                        break;
                    }
                }
            }
            Xpp3Dom exclusions = path.getChild("exclusions");
            if (exclusions != null) for (Xpp3Dom item : exclusions.getChildren()) {
                Exclusion exclusion = new Exclusion();
                exclusion.setGroupId(value(item, "groupId"));
                exclusion.setArtifactId(value(item, "artifactId"));
                dependency.addExclusion(exclusion);
            }
            request.addDependency(RepositoryUtils.toDependency(dependency, session.getArtifactTypeRegistry()));
        }
        if (manageTransitives && project.getDependencyManagement() != null) {
            for (Dependency dependency : project.getDependencyManagement().getDependencies()) {
                request.addManagedDependency(RepositoryUtils.toDependency(dependency, session.getArtifactTypeRegistry()));
            }
        }
        try {
            LinkedHashSet<String> resolved = new LinkedHashSet<>();
            for (ArtifactResult artifact : resolver.resolveDependencies(session,
                    new DependencyRequest(request, DependencyFilterUtils.classpathFilter("runtime")))
                    .getArtifactResults()) {
                resolved.add(artifact.getArtifact().getFile().getCanonicalPath());
            }
            return new ArrayList<>(resolved);
        } catch (DependencyResolutionException | IOException error) {
            throw new MojoExecutionException("Unable to resolve annotationProcessorPaths", error);
        }
    }

    private static String value(Xpp3Dom parent, String name) {
        Xpp3Dom child = parent.getChild(name);
        return child == null || child.getValue() == null ? "" : child.getValue().trim();
    }
}
