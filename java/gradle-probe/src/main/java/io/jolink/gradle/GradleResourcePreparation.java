package io.jolink.gradle;

import java.io.File;
import java.io.IOException;
import java.util.*;
import org.gradle.api.Project;
import org.gradle.api.Task;
import org.gradle.api.tasks.AbstractCopyTask;
import org.gradle.api.tasks.SourceSet;

/** Use the actual configured Copy task, including filters, renames and dependencies. */
final class GradleResourcePreparation {
    static void describe(Project project, SourceSet source, Map<String, Object> model) throws IOException {
        Task task = project.getTasks().getByName(source.getProcessResourcesTaskName());
        Set<String> inputs = new LinkedHashSet<>();
        for (File root : source.getResources().getSrcDirs()) inputs.add(root.getCanonicalPath());
        for (File input : task.getInputs().getFiles().getFiles()) inputs.add(input.getCanonicalPath());
        if (task instanceof AbstractCopyTask) {
            ((AbstractCopyTask) task).getSource().getAsFileTree().visit(detail -> {
                File root = detail.getFile();
                for (String ignored : detail.getRelativePath().getSegments()) root = root.getParentFile();
                inputs.add(root.getAbsolutePath());
            });
        }
        List<String> outputs = new ArrayList<>();
        for (File output : task.getOutputs().getFiles().getFiles()) {
            outputs.add(output.getCanonicalPath());
            inputs.add(output.getCanonicalPath());
        }
        model.put("preparedResourceDirectories", outputs);
        model.put("resourcePreparationInputs", new ArrayList<>(inputs));
    }
}
