package io.jolink.gradle;

import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;
import org.gradle.api.internal.GeneratedSubclasses;
import org.gradle.api.internal.TaskInputsInternal;
import org.gradle.api.internal.file.FileCollectionInternal;
import org.gradle.api.internal.project.ProjectInternal;
import org.gradle.api.internal.project.taskfactory.TaskActionFactory;
import org.gradle.api.internal.project.taskfactory.TaskClassInfo;
import org.gradle.api.internal.project.taskfactory.TaskClassInfoStore;
import org.gradle.api.tasks.compile.JavaCompile;
import org.gradle.internal.reflect.Instantiator;
import org.gradle.internal.service.ServiceRegistry;

/** Compare the configured task to the stock compiler of this Gradle distribution. */
final class JavaCompileActions {
    private List<Class<?>> stock;

    void describe(JavaCompile task, Map<String, Object> model) throws Exception {
        if (stock == null) {
            ServiceRegistry services = ((ProjectInternal) task.getProject()).getServices();
            Instantiator instances = services.get(Instantiator.class);
            TaskClassInfo info = services.get(TaskClassInfoStore.class).getTaskClassInfo(JavaCompile.class);
            // gradleApi() relocates this method's Guava ImmutableList return
            // type; the actual Gradle distribution does not. Use the common List.
            List<?> factories = (List<?>) TaskClassInfo.class.getMethod("getTaskActionFactories").invoke(info);
            stock = new ArrayList<>();
            for (Object f : factories) stock.add(((TaskActionFactory) f).create(instances).getClass());
        }
        if (GeneratedSubclasses.unpackType(task) == JavaCompile.class &&
                stock.equals(task.getActions().stream().map(Object::getClass).collect(Collectors.toList()))) return;

        model.put("customCompileActions", true);
        // Only inputs registered by the build, not JavaCompile's annotated source
        // and classpath. Keep the file tree (including its filters) even when empty.
        DeclaredOutputs.Inputs inputs = new DeclaredOutputs.Inputs();
        for (Method visit : TaskInputsInternal.class.getMethods()) {
            if (!visit.getName().equals("visitRegisteredProperties")) continue;
            // Gradle 7 and 8 moved the visitor interface and changed its method
            // signature. Both end the file-property callback with (value, fileType).
            Class<?> visitor = visit.getParameterTypes()[0];
            visit.invoke(task.getInputs(), Proxy.newProxyInstance(visitor.getClassLoader(), new Class<?>[]{visitor},
                    (proxy, method, args) -> {
                        if (method.getName().equals("visitInputFileProperty"))
                            ((FileCollectionInternal) task.getProject().files(args[args.length - 2])).visitStructure(inputs);
                        return null;
                    }));
            Set<String> generated = new LinkedHashSet<>();
            inputs.followProducers(task, new HashSet<>(), generated);
            model.put("extraGeneratedPaths", generated);
            inputs.exportTo(model);
            return;
        }
        throw new IllegalStateException("Gradle cannot expose registered JavaCompile inputs: " + task.getPath());
    }
}
