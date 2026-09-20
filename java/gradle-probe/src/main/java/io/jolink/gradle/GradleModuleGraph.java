package io.jolink.gradle;

import java.io.File;
import java.io.IOException;
import java.util.*;
import org.gradle.api.Project;
import org.gradle.api.GradleException;
import org.gradle.api.artifacts.Configuration;
import org.gradle.api.artifacts.component.ProjectComponentIdentifier;
import org.gradle.api.artifacts.result.ResolvedArtifactResult;
import org.gradle.api.tasks.SourceSet;
import org.gradle.api.tasks.SourceSetContainer;
import org.gradle.api.tasks.bundling.Jar;

/** Follow Gradle's resolved project artifacts; never run their producing tasks. */
final class GradleModuleGraph {
    final Map<String, Project> projects = new LinkedHashMap<>();
    final Map<String, Map<String, String>> artifacts = new LinkedHashMap<>();

    GradleModuleGraph(Project target, boolean tests) throws IOException {
        List<Project> pending = new ArrayList<>();
        pending.add(target);
        for (int i = 0; i < pending.size(); i++) {
            Project project = pending.get(i);
            if (projects.putIfAbsent(project.getPath(), project) != null) continue;
            SourceSetContainer sets = project.getExtensions().getByType(SourceSetContainer.class);
            List<SourceSet> sources = new ArrayList<>();
            sources.add(sets.getByName("main"));
            if (project.equals(target) && tests) sources.add(sets.getByName("test"));
            for (SourceSet source : sources) {
                for (String name : Arrays.asList(source.getCompileClasspathConfigurationName(),
                        source.getRuntimeClasspathConfigurationName(), source.getAnnotationProcessorConfigurationName())) {
                    Configuration configuration = project.getConfigurations().getByName(name);
                    for (ResolvedArtifactResult artifact : configuration.getIncoming().getArtifacts().getArtifacts()) {
                        if (!(artifact.getId().getComponentIdentifier() instanceof ProjectComponentIdentifier)) continue;
                        ProjectComponentIdentifier id = (ProjectComponentIdentifier) artifact.getId().getComponentIdentifier();
                        if (!id.getBuild().isCurrentBuild()) throw new GradleException(
                                "GRADLE_INCLUDED_BUILD_UNSUPPORTED: " + id.getDisplayName());
                        Project producer = project.getRootProject().project(id.getProjectPath());
                        SourceSet main = producer.getExtensions().getByType(SourceSetContainer.class).getByName("main");
                        String path = artifact.getFile().getCanonicalPath();
                        String kind;
                        if (main.getOutput().getClassesDirs().getFiles().contains(artifact.getFile())) kind = "classes";
                        else if (artifact.getFile().equals(main.getOutput().getResourcesDir())) kind = "resources";
                        else if (artifact.getFile().equals(((Jar) producer.getTasks().getByName(main.getJarTaskName()))
                                .getArchiveFile().get().getAsFile())) kind = "jar";
                        else throw new GradleException("GRADLE_PROJECT_ARTIFACT_UNSUPPORTED: " + artifact.getId().getDisplayName());
                        Map<String, String> facts = new LinkedHashMap<>();
                        facts.put("projectDirectory", producer.getProjectDir().getCanonicalPath());
                        facts.put("kind", kind);
                        artifacts.put(path, facts);
                        pending.add(producer);
                    }
                }
            }
        }
    }
}
