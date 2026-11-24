package io.jolink.gradle;

import java.io.File;
import java.io.IOException;
import java.util.*;
import org.gradle.api.Project;
import org.gradle.api.Task;
import org.gradle.api.GradleException;
import org.gradle.api.artifacts.Configuration;
import org.gradle.api.artifacts.component.ProjectComponentIdentifier;
import org.gradle.api.artifacts.result.ResolvedArtifactResult;
import org.gradle.api.tasks.SourceSet;
import org.gradle.api.tasks.SourceSetContainer;
import org.gradle.api.tasks.bundling.Jar;
import org.gradle.api.tasks.compile.JavaCompile;

/** Follow Gradle's resolved project artifacts; never run their producing tasks. */
final class GradleModuleGraph {
    final Map<String, Project> projects = new LinkedHashMap<>();
    final Map<String, Map<String, String>> artifacts = new LinkedHashMap<>();
    final Map<Project, Set<SourceSet>> scopes = new LinkedHashMap<>();
    final Set<SourceSet> variants = new LinkedHashSet<>();
    final Map<SourceSet, Jar> selectedJars = new LinkedHashMap<>();

    GradleModuleGraph(Project target, boolean tests) throws IOException {
        List<Map.Entry<Project, SourceSet>> pending = new ArrayList<>();
        SourceSetContainer initial = target.getExtensions().getByType(SourceSetContainer.class);
        select(target, initial.getByName("main"), pending);
        if (tests) select(target, initial.getByName("test"), pending);
        for (int i = 0; i < pending.size(); i++) {
            Project project = pending.get(i).getKey();
            SourceSet source = pending.get(i).getValue();
            for (String name : Arrays.asList(source.getCompileClasspathConfigurationName(),
                    source.getRuntimeClasspathConfigurationName(), source.getAnnotationProcessorConfigurationName())) {
                Configuration configuration = project.getConfigurations().getByName(name);
                for (ResolvedArtifactResult artifact : configuration.getIncoming().getArtifacts().getArtifacts()) {
                    if (!(artifact.getId().getComponentIdentifier() instanceof ProjectComponentIdentifier)) continue;
                    ProjectComponentIdentifier id = (ProjectComponentIdentifier) artifact.getId().getComponentIdentifier();
                    if (!id.getBuild().isCurrentBuild()) throw new GradleException(
                            "GRADLE_INCLUDED_BUILD_UNSUPPORTED: " + id.getDisplayName());
                    Project producer = project.getRootProject().project(id.getProjectPath());
                    SourceSetContainer sets = producer.getExtensions().getByType(SourceSetContainer.class);
                    String path = artifact.getFile().getCanonicalPath();
                    if (artifacts.containsKey(path)) continue;
                    SourceSet main = sets.getByName("main");
                    SourceSet owner = main;
                    String kind = outputKind(producer, owner, artifact.getFile());
                    if (kind == null) for (SourceSet candidate : sets) {
                        if (candidate == main) continue;
                        kind = outputKind(producer, candidate, artifact.getFile());
                        if (kind != null) { owner = candidate; break; }
                    }
                    if (kind == null) throw new GradleException("GRADLE_PROJECT_ARTIFACT_UNSUPPORTED: " + artifact.getId().getDisplayName());
                    Map<String, String> facts = new LinkedHashMap<>();
                    facts.put("projectDirectory", producer.getProjectDir().getCanonicalPath());
                    facts.put("kind", kind);
                    facts.put("sourceSet", owner.getName());
                    artifacts.put(path, facts);
                    if (owner != main) variants.add(owner);
                    select(producer, owner, pending);
                }
            }
        }
    }

    private void select(Project p, SourceSet s, List<Map.Entry<Project, SourceSet>> pending) {
        projects.putIfAbsent(p.getPath(), p);
        if (scopes.computeIfAbsent(p, ignored -> new LinkedHashSet<>()).add(s))
            pending.add(new AbstractMap.SimpleImmutableEntry<>(p, s));
    }

    private String outputKind(Project p, SourceSet s, File file) {
        if (s.getOutput().getClassesDirs().getFiles().contains(file)) return "classes";
        if (s.getOutput().getDirs().getFiles().contains(file)) return "extra";
        if (file.equals(s.getOutput().getResourcesDir())) return "resources";
        Task jar = p.getTasks().findByName(s.getJarTaskName());
        if (jar instanceof Jar && file.equals(((Jar) jar).getArchiveFile().get().getAsFile())) {
            // If javac writes a versioned descriptor, the ordinary classes
            // root cannot substitute for the resolved multi-release JAR.
            Task compiler = p.getTasks().getByName(s.getCompileJavaTaskName());
            if (!s.getName().equals("main") || (compiler instanceof JavaCompile &&
                    !s.getOutput().getClassesDirs().contains(((JavaCompile)compiler).getDestinationDirectory().get().getAsFile())))
                selectedJars.put(s, (Jar) jar);
            return "jar";
        }
        return null;
    }
}
