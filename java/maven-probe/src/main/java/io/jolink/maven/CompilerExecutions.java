package io.jolink.maven;

import java.io.File;
import java.util.*;
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

        void add(String id, Xpp3Dom selected) {
            if (ids.isEmpty()) configuration = selected;
            else if (!canonical(configuration).toString().equals(canonical(selected).toString())) ambiguous = true;
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
            for (MojoExecution execution : plans.calculateExecutionPlan(session, project, tasks, false).getMojoExecutions()) {
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
                ("compile".equals(goal) ? result.main : result.test).add(execution.getExecutionId(), selected);
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

    private static Xpp3Dom canonical(Xpp3Dom source) {
        Xpp3Dom result = new Xpp3Dom(source.getName());
        result.setValue(source.getValue());
        String[] attributes = source.getAttributeNames();
        Arrays.sort(attributes);
        for (String attribute : attributes) result.setAttribute(attribute, source.getAttribute(attribute));
        Xpp3Dom[] children = source.getChildren();
        // Order among equal-name entries (compiler args, processor paths) is
        // meaningful; ordering distinct scalar field names is not.
        Arrays.sort(children, Comparator.comparing(Xpp3Dom::getName));
        for (Xpp3Dom child : children) result.addChild(canonical(child));
        return result;
    }
}
