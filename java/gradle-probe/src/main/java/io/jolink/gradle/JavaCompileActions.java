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
import org.gradle.process.CommandLineArgumentProvider;

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
        boolean custom = GeneratedSubclasses.unpackType(task) != JavaCompile.class ||
                !stock.equals(task.getActions().stream().map(Object::getClass).collect(Collectors.toList()));
        if (custom) model.put("customCompileActions", true);
        List<CommandLineArgumentProvider> providers = task.getOptions().getCompilerArgumentProviders();
        if (!custom && providers.isEmpty() && task.getOptions().getCompilerArgs().isEmpty()) return;
        // Only inputs registered by the build, not JavaCompile's annotated source
        // and classpath. Keep the file tree (including its filters) even when empty.
        DeclaredOutputs.Inputs inputs = new DeclaredOutputs.Inputs();
        for (Method visit : TaskInputsInternal.class.getMethods()) {
            if (!visit.getName().equals("visitRegisteredProperties")) continue;
            // Gradle 7 and 8 moved the visitor interface and changed its method
            // signature. Both end the file-property callback with (value, fileType).
            Class<?> visitor = visit.getParameterTypes()[0];
            Object collector = Proxy.newProxyInstance(visitor.getClassLoader(), new Class<?>[]{visitor},
                    (proxy, method, args) -> {
                        if (method.getName().equals("visitInputFileProperty"))
                            ((FileCollectionInternal) task.getProject().files(args[args.length - 2])).visitStructure(inputs);
                        return null;
                    });
            if (!providers.isEmpty()) {
                // Ask Gradle to visit just the providers' annotated properties (not
                // the whole compiler classpath). File/directory inputs can change
                // the emitted flags without any change to sources or build scripts.
                String pkg = visitor.getPackage().getName();
                Class<?> type = Class.forName(pkg + (pkg.equals("org.gradle.internal.properties")
                        ? ".bean.PropertyWalker" : ".PropertyWalker"), false, visitor.getClassLoader());
                Object walker = ((ProjectInternal) task.getProject()).getServices().get(type);
                Method walk = java.util.Arrays.stream(type.getMethods())
                        .filter(m -> m.getName().equals("visitProperties") && m.getParameterCount() == 3).findFirst().get();
                Object validation = walk.getParameterTypes()[1].getField("NOOP").get(null);
                for (CommandLineArgumentProvider provider : providers) walk.invoke(walker, provider, validation, collector);
                if (!inputs.files.isEmpty() || !inputs.trees.isEmpty()) model.put("fileBackedCompilerArguments", true);
            }
            visit.invoke(task.getInputs(), collector);
            Set<String> generated = new LinkedHashSet<>();
            inputs.followProducers(task, new HashSet<>(), generated);
            model.put("extraGeneratedPaths", generated);
            inputs.exportTo(model);
            return;
        }
        throw new IllegalStateException("Gradle cannot expose registered JavaCompile inputs: " + task.getPath());
    }
}
