package io.jolink.gradle;

import java.io.File;
import java.io.IOException;
import java.nio.file.Path;
import java.util.*;
import org.gradle.api.Project;
import org.gradle.api.Task;
import org.gradle.api.internal.file.FileCollectionInternal;
import org.gradle.api.internal.file.FileCollectionStructureVisitor;
import org.gradle.api.internal.file.FileTreeInternal;
import org.gradle.api.internal.file.collections.FileSystemMirroringFileTree;
import org.gradle.api.tasks.SourceSet;
import org.gradle.api.tasks.util.PatternSet;

/** Facts for directories registered via SourceSet.output.dir, not arbitrary task actions. */
final class DeclaredOutputs {
    static void describe(Project project, SourceSet source, Map<String, Object> model) throws IOException {
        Set<File> dirs = source.getOutput().getDirs().getFiles();
        if (dirs.isEmpty()) return;
        Set<String> extra = paths(dirs);
        Task classes = project.getTasks().getByName(source.getClassesTaskName());
        Set<? extends Task> builtBy = source.getOutput().getDirs().getBuildDependencies().getDependencies(null);
        Set<Task> standard = new HashSet<>();
        Set<String> classDirs = paths(source.getOutput().getClassesDirs().getFiles());
        // Older Gradle also includes unrelated lifecycle dependencies here.
        // Only cut off producers which really own a standard classes output.
        for (Task task : source.getOutput().getClassesDirs().getBuildDependencies().getDependencies(null)) {
            if (!Collections.disjoint(paths(task.getOutputs().getFiles().getFiles()), classDirs))
                standard.add(task);
        }
        standard.add(project.getTasks().getByName(source.getCompileJavaTaskName()));
        standard.add(project.getTasks().getByName(source.getProcessResourcesTaskName()));
        standard.remove(classes);
        standard.removeAll(builtBy);

        // A project can wire its producer into classes.dependsOn rather than
        // output.dir(builtBy: ...). Its destination can be below the registered root.
        Set<Task> related = new LinkedHashSet<>();
        collect(classes, standard, related);
        for (Task task : builtBy) collect(task, standard, related);
        Set<Task> producers = new LinkedHashSet<>();
        Set<String> unowned = new LinkedHashSet<>(extra);
        for (Task task : related) {
            if (task == classes) continue;
            Set<String> destinations = paths(task.getOutputs().getFiles().getFiles());
            boolean matches = false;
            for (String dir : extra) {
                Path root = new File(dir).toPath();
                for (String destination : destinations) {
                    Path out = new File(destination).toPath();
                    if (root.startsWith(out)) unowned.remove(dir);
                    // A nested destination only owns its own subtree.
                    if (root.startsWith(out) || out.startsWith(root)) matches = true;
                }
            }
            if (matches || builtBy.contains(task)) collect(task, standard, producers);
        }
        Set<String> tasks = new LinkedHashSet<>();
        for (Task task : builtBy) tasks.add(task.getPath());
        Inputs inputs = new Inputs();
        Set<String> generated = new LinkedHashSet<>();
        for (Task task : producers) {
            generated.addAll(paths(task.getOutputs().getFiles().getFiles()));
            ((FileCollectionInternal)task.getInputs().getFiles()).visitStructure(inputs);
        }
        model.put("extraOutputDirs", extra);
        model.put("extraOutputTasks", tasks);
        inputs.exportTo(model);
        model.put("extraGeneratedPaths", generated);
        model.put("extraUnownedDirs", unowned);
    }

    private static Set<String> paths(Collection<File> files) throws IOException {
        Set<String> result = new LinkedHashSet<>();
        for (File f : files) result.add(f.getCanonicalPath());
        return result;
    }

    private static void collect(Task task, Set<Task> standard, Set<Task> seen) {
        if (standard.contains(task) || !seen.add(task)) return;
        for (Task dep : task.getTaskDependencies().getDependencies(task)) collect(dep, standard, seen);
    }

    /** Keep file-tree roots and patterns even if no file currently matches. */
    static final class Inputs implements FileCollectionStructureVisitor {
        final Set<String> files = new LinkedHashSet<>();
        final Set<Map<String, Object>> trees = new LinkedHashSet<>();
        void exportTo(Map<String, Object> model) {
            model.put("extraInputFiles", files);
            model.put("extraInputTrees", trees);
        }
        /** Track original inputs when a registered input is itself another task's output. */
        void followProducers(Task consumer, Set<Task> seen, Set<String> generated) throws IOException {
            if (files.isEmpty() && trees.isEmpty()) return;
            Set<Path> roots = new LinkedHashSet<>();
            for (String file : files) roots.add(new File(file).toPath());
            for (Map<String, Object> tree : trees) roots.add(new File((String) tree.get("directory")).toPath());
            for (Task task : consumer.getTaskDependencies().getDependencies(consumer)) {
                if (seen.contains(task)) continue;
                Set<String> outputs = paths(task.getOutputs().getFiles().getFiles());
                boolean feedsInput = outputs.stream().map(p -> new File(p).toPath())
                        .anyMatch(out -> roots.stream().anyMatch(in -> in.startsWith(out) || out.startsWith(in)));
                if (!feedsInput) continue;
                seen.add(task);
                generated.addAll(outputs);
                Inputs upstream = new Inputs();
                ((FileCollectionInternal) task.getInputs().getFiles()).visitStructure(upstream);
                upstream.followProducers(task, seen, generated);
                files.addAll(upstream.files);
                trees.addAll(upstream.trees);
            }
        }
        private String canonical(File f) {
            try { return f.getCanonicalPath(); }
            catch (IOException e) { throw new org.gradle.api.GradleException("Cannot resolve producer input " + f, e); }
        }
        public void visitCollection(FileCollectionInternal.Source ignored, Iterable<File> entries) {
            for (File f : entries) files.add(canonical(f));
        }
        public void visitFileTree(File dir, PatternSet patterns, FileTreeInternal tree) {
            Map<String, Object> input = new LinkedHashMap<>();
            input.put("directory", canonical(dir));
            input.put("includes", patterns.getIncludes());
            input.put("excludes", patterns.getExcludes());
            if (!patterns.isCaseSensitive()) input.put("caseSensitive", false);
            trees.add(input);
        }
        public void visitFileTreeBackedByFile(File f, FileTreeInternal tree, FileSystemMirroringFileTree mirror) {
            files.add(canonical(f));
        }
        // Also part of the visitor interface on Gradle 7.
        public void visitGenericFileTree(FileTreeInternal tree, FileSystemMirroringFileTree mirror) {
            visitCollection(FileCollectionInternal.OTHER, tree);
        }
    }
}
