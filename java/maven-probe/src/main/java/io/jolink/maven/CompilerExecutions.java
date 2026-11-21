package io.jolink.maven;

import java.io.File;
import java.util.*;
import javax.tools.JavaCompiler;
import javax.tools.ToolProvider;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.lifecycle.internal.LifecycleExecutionPlanCalculator;
import org.apache.maven.lifecycle.internal.LifecycleTask;
import org.apache.maven.plugin.MojoExecution;
import org.apache.maven.plugin.MojoExecutionException;
import org.apache.maven.plugin.PluginParameterExpressionEvaluator;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/** Select compiler inputs from Maven's lifecycle plan without running a compiler. */
final class CompilerExecutions {
    static final class Scope {
        final List<String> ids = new ArrayList<>();
        Xpp3Dom configuration = new Xpp3Dom("configuration");
        boolean ambiguous;
        File generatedSourceDirectory;
        MojoExecution execution;
        String moduleName;
        boolean strictComparison;

        void add(String id, Xpp3Dom selected, File generatedDirectory, boolean strict) {
            if (ids.isEmpty()) {
                configuration = selected;
                generatedSourceDirectory = generatedDirectory;
                strictComparison = strict;
            } else if (!Objects.equals(generatedSourceDirectory, generatedDirectory)
                    || !equivalent(configuration, selected, strictComparison || strict)) ambiguous = true;
            ids.add(id);
        }
    }

    final Scope main = new Scope();
    final Scope test = new Scope();

    static CompilerExecutions resolve(MavenSession session, MavenProject project,
            LifecycleExecutionPlanCalculator plans) throws MojoExecutionException {
        CompilerExecutions result = new CompilerExecutions();
        MavenProject previous = session.getCurrentProject();
        try {
            session.setCurrentProject(project);
            List<Object> tasks = Collections.singletonList(new LifecycleTask("test-compile"));
            boolean toolchainInPlan = false;
            for (MojoExecution execution : plans.calculateExecutionPlan(session, project, tasks, false).getMojoExecutions()) {
                if ("org.apache.maven.plugins".equals(execution.getGroupId())
                        && "maven-toolchains-plugin".equals(execution.getArtifactId())) toolchainInPlan = true;
                if (!"org.apache.maven.plugins".equals(execution.getGroupId())
                        || !"maven-compiler-plugin".equals(execution.getArtifactId())) continue;
                String goal = execution.getGoal();
                if (!"compile".equals(goal) && !"testCompile".equals(goal)) continue;
                // The native plan excludes phase=none and inactive executions.
                // Maven also owns plugin/execution configuration inheritance.
                Xpp3Dom selected = project.getGoalConfiguration(execution.getGroupId(),
                        execution.getArtifactId(), execution.getExecutionId(), goal);
                selected = selected == null ? new Xpp3Dom("configuration") : new Xpp3Dom(selected);
                evaluate(selected, new PluginParameterExpressionEvaluator(session, execution));
                // Obtain the plugin's actual parameter defaults as well as user
                // overrides, without executing its compiler goal.
                plans.setupMojoExecution(session, project, execution);
                PluginParameterExpressionEvaluator evaluator = new PluginParameterExpressionEvaluator(session, execution);
                String parameter = "compile".equals(goal) ? "generatedSourcesDirectory" : "generatedTestSourcesDirectory";
                Xpp3Dom node = execution.getConfiguration().getChild(parameter);
                File generated = null;
                if (node != null) {
                    String expression = node.getValue() == null ? node.getAttribute("default-value") : node.getValue();
                    if (expression != null) {
                        Object value = evaluator.evaluate(expression);
                        if (value != null) generated = evaluator.alignToBaseDirectory(
                            value instanceof File ? (File) value : new File(value.toString())).getCanonicalFile();
                    }
                }
                Scope scope = "compile".equals(goal) ? result.main : result.test;
                // Option arity/validity below comes from the Maven JVM's javac.
                // A fork/toolchain can use a different JDK (even with compilerId=javac).
                scope.add(execution.getExecutionId(), selected, generated,
                        toolchainInPlan || selected.getChild("jdkToolchain") != null
                        || !"false".equalsIgnoreCase(setting(execution, evaluator, "fork", "false"))
                        || !setting(execution, evaluator, "executable", "").isEmpty()
                        || !"false".equalsIgnoreCase(setting(execution, evaluator, "failOnWarning", "false"))
                        || !"javac".equals(setting(execution, evaluator, "compilerId", "javac")));
                if (scope.execution == null) scope.execution = execution;
            }
            return result;
        } catch (Exception error) {
            throw new MojoExecutionException("Unable to resolve active Maven compiler executions", error);
        } finally {
            session.setCurrentProject(previous);
        }
    }

    private static void evaluate(Xpp3Dom configuration, PluginParameterExpressionEvaluator evaluator)
            throws org.codehaus.plexus.component.configurator.expression.ExpressionEvaluationException {
        if (configuration.getValue() != null) {
            Object value = evaluator.evaluate(configuration.getValue());
            if (value instanceof String || value instanceof Number || value instanceof Boolean || value instanceof File)
                configuration.setValue(value.toString());
        }
        for (Xpp3Dom child : configuration.getChildren()) evaluate(child, evaluator);
    }

    private static String setting(MojoExecution execution, PluginParameterExpressionEvaluator evaluator,
                                  String name, String fallback) throws Exception {
        Xpp3Dom node = execution.getConfiguration().getChild(name);
        if (node == null) return fallback;
        String expression = node.getValue() == null ? node.getAttribute("default-value") : node.getValue();
        Object value = expression == null ? null : evaluator.evaluate(expression);
        return value == null ? fallback : value.toString();
    }

    /** Compare only; keep the original effective configuration for compilation and later validation. */
    private static boolean equivalent(Xpp3Dom left, Xpp3Dom right, boolean strict) {
        if (canonical(left).toString().equals(canonical(right).toString())) return true;
        if (strict) return false;
        JavaCompiler javac = ToolProvider.getSystemJavaCompiler();
        if (javac == null) return false;
        String first = optionalWarningKey(left, javac);
        return first != null && first.equals(optionalWarningKey(right, javac));
    }

    private static String optionalWarningKey(Xpp3Dom source, JavaCompiler javac) {
        // Do not guess how a free-form string, old argument map or argfile was tokenized.
        for (String key : Arrays.asList("compilerArgument", "compilerArguments", "testCompilerArgument",
                                       "testCompilerArguments", "testCompilerArgs"))
            if (source.getChild(key) != null) return null;
        Xpp3Dom copy = new Xpp3Dom(source);
        for (int i = copy.getChildCount() - 1; i >= 0; i--) {
            Xpp3Dom field = copy.getChild(i);
            if ("compilerArgs".equals(field.getName())) {
                if (field.getAttributeNames().length != 0 || field.getValue() != null) return null;
                for (Xpp3Dom arg : field.getChildren())
                    if (arg.getValue() != null && arg.getValue().startsWith("@")) return null;
                for (int j = 0; j < field.getChildCount();) {
                    Xpp3Dom arg = field.getChild(j);
                    String v = arg.getValue();
                    if (v == null || arg.getChildCount() != 0 || arg.getAttributeNames().length != 0
                            || (!"arg".equals(arg.getName()) && !"compilerArg".equals(arg.getName()))
                            || v.equals("-Werror") || v.startsWith("-Xplugin:")) return null;
                    int operands = javac.isSupportedOption(v);
                    if (operands < 0 || j + operands >= field.getChildCount()) return null;
                    if (operands == 0 && ("-nowarn".equals(v) || "-deprecation".equals(v)
                            || "-Xlint".equals(v) || v.startsWith("-Xlint:")))
                        field.removeChild(j);
                    else j += 1 + operands; // An operand that looks like a warning option is still an operand.
                }
                if (field.getChildCount() == 0 && field.getValue() == null) copy.removeChild(i);
            } else if (("showWarnings".equals(field.getName()) || "showDeprecation".equals(field.getName()))
                    && field.getAttributeNames().length == 0 && field.getChildCount() == 0 && ("true".equalsIgnoreCase(field.getValue())
                    || "false".equalsIgnoreCase(field.getValue()))) copy.removeChild(i);
        }
        return canonical(copy).toString();
    }

    private static Xpp3Dom canonical(Xpp3Dom source) {
        Xpp3Dom result = new Xpp3Dom(source.getName());
        result.setValue(source.getValue());
        String[] attributes = source.getAttributeNames();
        Arrays.sort(attributes);
        for (String attribute : attributes) result.setAttribute(attribute, source.getAttribute(attribute));
        Xpp3Dom[] children = source.getChildren();
        // Order among equal-name entries (compiler args, processor paths) is
        // meaningful; ordering distinct scalar field names is not.
        if (!"compilerArgs".equals(source.getName()))
            Arrays.sort(children, Comparator.comparing(Xpp3Dom::getName));
        for (Xpp3Dom child : children) result.addChild(canonical(child));
        return result;
    }
}
