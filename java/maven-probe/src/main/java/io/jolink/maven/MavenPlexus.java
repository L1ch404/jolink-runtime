package io.jolink.maven;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import org.apache.maven.execution.MavenSession;

/** Maven 3 ships Plexus Utils but does not export these packages through maven.api. */
final class MavenPlexus {
    private MavenPlexus() {}

    private static Method method(String type, String name, Class<?>... parameters) {
        try {
            return MavenSession.class.getClassLoader().loadClass("org.codehaus.plexus.util." + type)
                    .getMethod(name, parameters);
        } catch (ReflectiveOperationException e) {
            throw new IllegalStateException("Maven runtime is missing Plexus " + type + "." + name, e);
        }
    }

    private static final class Paths {
        static final Method MATCH = method("SelectorUtils", "matchPath", String.class, String.class);
    }
    private static final class Arguments {
        static final Method SPLIT = method("cli.CommandLineUtils", "translateCommandline", String.class);
    }

    static boolean matches(String pattern, String relative) throws ReflectiveOperationException {
        return (Boolean) Paths.MATCH.invoke(null, pattern, relative);
    }

    static String[] split(String line) {
        try {
            return (String[]) Arguments.SPLIT.invoke(null, line);
        } catch (InvocationTargetException e) {
            throw new IllegalArgumentException("Invalid compilerArgument quoting.", e.getCause());
        } catch (IllegalAccessException e) {
            throw new IllegalStateException("Cannot invoke Maven's command-line parser.", e);
        }
    }
}
