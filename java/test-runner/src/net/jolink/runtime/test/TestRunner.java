package net.jolink.runtime.test;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.File;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.lang.reflect.Array;
import java.lang.reflect.Constructor;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.net.InetAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.LinkedHashSet;

/** Isolated Java 8 runner for explicit JUnit 4/5 class and method selectors. */
public final class TestRunner {
    private static final int MAX_FAILURES = 8;
    private static final int MAX_TEXT = 8 * 1024;

    private TestRunner() {
    }

    public static void main(String[] args) {
        Map<String, String> options = parse(args);
        String host = options.get("host");
        String portText = options.get("port");
        String token = options.get("token");
        String runId = options.get("run-id");
        String framework = options.get("framework");
        String selectorsFile = options.get("selectors-file");
        String classpathFile = options.get("classpath-file");
        if (host == null || portText == null || token == null
                || runId == null || framework == null
                || selectorsFile == null || classpathFile == null) {
            System.exit(2);
            return;
        }
        try (Socket socket = new Socket(
                InetAddress.getByName(host), Integer.parseInt(portText));
             BufferedWriter protocol = new BufferedWriter(
                     new OutputStreamWriter(
                             socket.getOutputStream(), StandardCharsets.UTF_8))) {
            emit(protocol, hello(token, runId));
            List<String> selectors = readSelectors(Paths.get(selectorsFile));
            configureProjectClasspath(Paths.get(classpathFile));
            ClassLoader loader = ClassLoader.getSystemClassLoader();
            Thread.currentThread().setContextClassLoader(loader);
            Result result;
            if ("junit4".equals(framework)) {
                result = runJUnit4(selectors, loader);
            } else if ("junit5".equals(framework)) {
                result = runJUnit5(selectors, loader);
            } else if ("testng".equals(framework)) {
                result = runTestNG(selectors, loader);
            } else if ("auto".equals(framework)) {
                result = runAuto(selectors, loader);
            } else {
                throw new IllegalArgumentException(
                        "Unsupported test framework.");
            }
            emit(protocol, resultJson(
                    token,
                    runId,
                    result.framework == null ? framework : result.framework,
                    result));
        } catch (Throwable error) {
            try {
                sendInfrastructureFailure(
                        host,
                        portText,
                        token,
                        runId,
                        error
                );
            } catch (Throwable ignored) {
                // Python also treats a missing terminal frame as infrastructure failure.
            }
            System.exit(2);
            return;
        }
        // User tests may leave non-daemon pools, schedulers, or SDK threads.
        // The Test Runner is disposable and must terminate after its protocol
        // terminal frame has been flushed and the socket has been closed.
        System.exit(0);
    }

    private static Result runJUnit4(
            List<String> selectors, ClassLoader loader) throws Exception {
        Class<?> requestType = Class.forName(
                "org.junit.runner.Request", true, loader);
        Class<?> coreType = Class.forName(
                "org.junit.runner.JUnitCore", true, loader);
        Object core = coreType.getConstructor().newInstance();
        Method run = coreType.getMethod("run", requestType);
        Method classRequest = requestType.getMethod("aClass", Class.class);
        Method methodRequest = requestType.getMethod(
                "method", Class.class, String.class);
        Result total = new Result();
        total.framework = "junit4";
        for (String selector : selectors) {
            Selector parsed = Selector.parse(selector);
            Class<?> testClass = Class.forName(parsed.className, false, loader);
            Object request = parsed.methodName == null
                    ? classRequest.invoke(null, testClass)
                    : methodRequest.invoke(null, testClass, parsed.methodName);
            Object result = run.invoke(core, request);
            long runCount = number(result, "getRunCount");
            long failureCount = number(result, "getFailureCount");
            long ignoreCount = number(result, "getIgnoreCount");
            long assumptionCount = optionalNumber(
                    result, "getAssumptionFailureCount");
            total.tests += runCount + ignoreCount;
            total.failed += failureCount;
            total.failedTests += failureCount;
            total.skipped += ignoreCount + assumptionCount;
            total.passed += Math.max(
                    0, runCount - failureCount - assumptionCount);
            total.durationMs += number(result, "getRunTime");
            @SuppressWarnings("unchecked")
            List<Object> failures = (List<Object>) result.getClass()
                    .getMethod("getFailures").invoke(result);
            for (Object failure : failures) {
                total.addFailure(failure(
                        String.valueOf(failure.getClass()
                                .getMethod("getTestHeader").invoke(failure)),
                        (Throwable) failure.getClass()
                                .getMethod("getException").invoke(failure)));
            }
        }
        return total;
    }

    private static Result runJUnit5(
            List<String> selectors, ClassLoader loader) throws Exception {
        Class<?> builderType = Class.forName(
                "org.junit.platform.launcher.core.LauncherDiscoveryRequestBuilder",
                true, loader);
        Class<?> selectorsType = Class.forName(
                "org.junit.platform.engine.discovery.DiscoverySelectors",
                true, loader);
        Class<?> selectorType = Class.forName(
                "org.junit.platform.engine.DiscoverySelector", true, loader);
        Object builder = builderType.getMethod("request").invoke(null);
        Object selectorArray = Array.newInstance(selectorType, selectors.size());
        for (int index = 0; index < selectors.size(); index++) {
            Selector parsed = Selector.parse(selectors.get(index));
            Object selector = parsed.methodName == null
                    ? selectorsType.getMethod("selectClass", String.class)
                            .invoke(null, parsed.className)
                    : selectorsType.getMethod("selectMethod", String.class)
                            .invoke(null, junit5MethodSelector(parsed, loader));
            Array.set(selectorArray, index, selector);
        }
        builderType.getMethod("selectors", selectorArray.getClass())
                .invoke(builder, selectorArray);
        Object request = builderType.getMethod("build").invoke(builder);

        Class<?> listenerType = Class.forName(
                "org.junit.platform.launcher.listeners.SummaryGeneratingListener",
                true, loader);
        Object listener = listenerType.getConstructor().newInstance();
        Class<?> listenerInterface = Class.forName(
                "org.junit.platform.launcher.TestExecutionListener", true, loader);
        Object listenerArray = Array.newInstance(listenerInterface, 1);
        Array.set(listenerArray, 0, listener);
        Class<?> launcherType = Class.forName(
                "org.junit.platform.launcher.Launcher", true, loader);
        Object launcher = Class.forName(
                "org.junit.platform.launcher.core.LauncherFactory", true, loader)
                .getMethod("create").invoke(null);
        launcherType.getMethod(
                "registerTestExecutionListeners", listenerArray.getClass())
                .invoke(launcher, listenerArray);
        Method execute = findExecute(launcherType, request.getClass());
        if (execute.getParameterTypes().length == 1) {
            execute.invoke(launcher, request);
        } else {
            execute.invoke(
                    launcher,
                    request,
                    Array.newInstance(listenerInterface, 0));
        }

        Object summary = listenerType.getMethod("getSummary").invoke(listener);
        Class<?> summaryType = Class.forName(
                "org.junit.platform.launcher.listeners.TestExecutionSummary",
                true, loader);
        Result result = new Result();
        result.framework = "junit5";
        result.tests = number(summaryType, summary, "getTestsFoundCount");
        result.passed = number(summaryType, summary, "getTestsSucceededCount");
        result.failedTests = number(
                summaryType, summary, "getTestsFailedCount");
        result.failedContainers = number(
                summaryType, summary, "getContainersFailedCount");
        result.failed = number(summaryType, summary, "getTotalFailureCount");
        result.skipped = number(summaryType, summary, "getTestsSkippedCount")
                + number(summaryType, summary, "getTestsAbortedCount");
        long started = number(summaryType, summary, "getTimeStarted");
        long finished = number(summaryType, summary, "getTimeFinished");
        result.durationMs = Math.max(0, finished - started);
        @SuppressWarnings("unchecked")
        List<Object> failures = (List<Object>) summaryType
                .getMethod("getFailures").invoke(summary);
        Class<?> failureType = Class.forName(
                "org.junit.platform.launcher.listeners.TestExecutionSummary$Failure",
                true, loader);
        Class<?> identifierType = Class.forName(
                "org.junit.platform.launcher.TestIdentifier", true, loader);
        for (Object item : failures) {
            Object identifier = failureType
                    .getMethod("getTestIdentifier").invoke(item);
            String name = String.valueOf(identifierType
                    .getMethod("getDisplayName").invoke(identifier));
            Throwable error = (Throwable) failureType
                    .getMethod("getException").invoke(item);
            result.addFailure(failure(name, error));
        }
        return result;
    }

    private static String junit5MethodSelector(
            Selector selector, ClassLoader loader) throws Exception {
        Class<?> type = Class.forName(selector.className, false, loader);
        List<Method> matches = new ArrayList<Method>();
        for (Method method : type.getDeclaredMethods()) {
            if (selector.methodName.equals(method.getName())) {
                matches.add(method);
            }
        }
        if (matches.size() > 1) {
            throw new IllegalArgumentException(
                    "An overloaded JUnit 5 method requires a signature selector.");
        }
        if (matches.size() != 1
                || matches.get(0).getParameterTypes().length == 0) {
            return selector.className + "#" + selector.methodName;
        }
        StringBuilder value = new StringBuilder(selector.className)
                .append('#').append(selector.methodName).append('(');
        Class<?>[] parameters = matches.get(0).getParameterTypes();
        for (int index = 0; index < parameters.length; index++) {
            if (index > 0) {
                value.append(',');
            }
            value.append(parameters[index].getTypeName());
        }
        return value.append(')').toString();
    }

    private static Result runAuto(
            List<String> selectors, ClassLoader loader) throws Exception {
        List<String> junit4 = new ArrayList<String>();
        List<String> junit5 = new ArrayList<String>();
        List<String> testng = new ArrayList<String>();
        for (String selector : selectors) {
            String framework = selectorFramework(selector, loader);
            if ("junit4".equals(framework)) {
                junit4.add(selector);
            } else if ("junit5".equals(framework)) {
                junit5.add(selector);
            } else if ("testng".equals(framework)) {
                testng.add(selector);
            } else {
                throw new IllegalArgumentException(
                        "The selected test framework is ambiguous or unsupported.");
            }
        }
        Result total = new Result();
        if (!junit4.isEmpty()) {
            total.merge(runJUnit4(junit4, loader));
        }
        if (!junit5.isEmpty()) {
            total.merge(runJUnit5(junit5, loader));
        }
        if (!testng.isEmpty()) {
            total.merge(runTestNG(testng, loader));
        }
        int frameworkCount = (!junit4.isEmpty() ? 1 : 0)
                + (!junit5.isEmpty() ? 1 : 0)
                + (!testng.isEmpty() ? 1 : 0);
        total.framework = frameworkCount > 1
                ? "mixed"
                : !junit5.isEmpty() ? "junit5"
                : !testng.isEmpty() ? "testng" : "junit4";
        return total;
    }

    private static Result runTestNG(
            List<String> selectors, ClassLoader loader) throws Exception {
        Class<?> testngType = Class.forName("org.testng.TestNG", true, loader);
        Class<?> suiteType = Class.forName(
                "org.testng.xml.XmlSuite", true, loader);
        Class<?> testType = Class.forName(
                "org.testng.xml.XmlTest", true, loader);
        Class<?> classType = Class.forName(
                "org.testng.xml.XmlClass", true, loader);
        Class<?> includeType = Class.forName(
                "org.testng.xml.XmlInclude", true, loader);
        Object suite = suiteType.getConstructor().newInstance();
        suiteType.getMethod("setName", String.class)
                .invoke(suite, "joLink Fast Test");
        Object test = testType.getConstructor(suiteType).newInstance(suite);
        testType.getMethod("setName", String.class)
                .invoke(test, "explicit selectors");

        Map<String, Set<String>> selected =
                new LinkedHashMap<String, Set<String>>();
        Set<String> wholeClasses = new LinkedHashSet<String>();
        for (String raw : selectors) {
            Selector selector = Selector.parse(raw);
            if (selector.methodName == null) {
                wholeClasses.add(selector.className);
            } else {
                Set<String> methods = selected.get(selector.className);
                if (methods == null) {
                    methods = new LinkedHashSet<String>();
                    selected.put(selector.className, methods);
                }
                methods.add(selector.methodName);
            }
        }
        for (String className : wholeClasses) {
            selected.put(className, Collections.<String>emptySet());
        }
        List<Object> classes = new ArrayList<Object>();
        for (Map.Entry<String, Set<String>> item : selected.entrySet()) {
            Class<?> selectedClass = Class.forName(
                    item.getKey(), false, loader);
            Object xmlClass = classType.getConstructor(Class.class)
                    .newInstance(selectedClass);
            if (!item.getValue().isEmpty()) {
                List<Object> includes = new ArrayList<Object>();
                for (String method : item.getValue()) {
                    includes.add(includeType.getConstructor(String.class)
                            .newInstance(method));
                }
                classType.getMethod("setIncludedMethods", List.class)
                        .invoke(xmlClass, includes);
            }
            classes.add(xmlClass);
        }
        testType.getMethod("setXmlClasses", List.class).invoke(test, classes);

        Object listener = Class.forName(
                "org.testng.TestListenerAdapter", true, loader)
                .getConstructor().newInstance();
        Object testng = testngType.getConstructor().newInstance();
        try {
            testngType.getMethod("setUseDefaultListeners", boolean.class)
                    .invoke(testng, false);
        } catch (NoSuchMethodException ignored) {
            // Older supported TestNG versions may omit this presentation option.
        }
        testngType.getMethod("addListener", Object.class)
                .invoke(testng, listener);
        testngType.getMethod("setXmlSuites", List.class)
                .invoke(testng, Collections.singletonList(suite));
        long started = System.currentTimeMillis();
        testngType.getMethod("run").invoke(testng);

        Class<?> listenerType = listener.getClass();
        @SuppressWarnings("unchecked")
        List<Object> passed = (List<Object>) listenerType
                .getMethod("getPassedTests").invoke(listener);
        @SuppressWarnings("unchecked")
        List<Object> failed = (List<Object>) listenerType
                .getMethod("getFailedTests").invoke(listener);
        @SuppressWarnings("unchecked")
        List<Object> skipped = (List<Object>) listenerType
                .getMethod("getSkippedTests").invoke(listener);
        @SuppressWarnings("unchecked")
        List<Object> partial = (List<Object>) listenerType
                .getMethod("getFailedButWithinSuccessPercentageTests")
                .invoke(listener);
        @SuppressWarnings("unchecked")
        List<Object> configurationFailures = (List<Object>) listenerType
                .getMethod("getConfigurationFailures").invoke(listener);
        Result result = new Result();
        result.framework = "testng";
        result.tests = passed.size() + failed.size() + skipped.size()
                + partial.size();
        result.passed = passed.size();
        result.failedTests = failed.size() + partial.size();
        result.failedContainers = configurationFailures.size();
        result.failed = result.failedTests + result.failedContainers;
        result.skipped = skipped.size();
        result.durationMs = Math.max(
                0, System.currentTimeMillis() - started);
        Class<?> testResultType = Class.forName(
                "org.testng.ITestResult", true, loader);
        for (Object item : failed) {
            result.addFailure(testNgFailure(item, testResultType));
        }
        for (Object item : partial) {
            result.addFailure(testNgFailure(item, testResultType));
        }
        for (Object item : configurationFailures) {
            result.addFailure(testNgFailure(item, testResultType));
        }
        return result;
    }

    private static Failure testNgFailure(Object item, Class<?> resultType)
            throws Exception {
        String name = String.valueOf(
                resultType.getMethod("getName").invoke(item));
        Throwable error = (Throwable) resultType
                .getMethod("getThrowable").invoke(item);
        if (error == null) {
            error = new AssertionError("TestNG reported a failure without a cause.");
        }
        return failure(name, error);
    }

    private static String selectorFramework(
            String raw, ClassLoader loader) throws Exception {
        Selector selector = Selector.parse(raw);
        Class<?> type = Class.forName(selector.className, false, loader);
        Set<String> frameworks = new LinkedHashSet<String>();
        for (java.lang.annotation.Annotation annotation : type.getAnnotations()) {
            recordFramework(
                    annotation.annotationType(),
                    frameworks,
                    new LinkedHashSet<Class<?>>(),
                    0);
        }
        recordMethodFrameworks(
                type, selector.methodName, frameworks,
                new LinkedHashSet<Class<?>>());
        if (frameworks.size() != 1) {
            return "unknown";
        }
        return frameworks.iterator().next();
    }

    private static boolean recordMethodFrameworks(
            Class<?> type,
            String methodName,
            Set<String> frameworks,
            Set<Class<?>> visitedTypes) {
        if (type == null || type == Object.class || !visitedTypes.add(type)) {
            return false;
        }
        boolean namedDeclarationFound = false;
        for (Method method : type.getDeclaredMethods()) {
            if (methodName != null && !methodName.equals(method.getName())) {
                continue;
            }
            namedDeclarationFound = true;
            for (java.lang.annotation.Annotation annotation
                    : method.getAnnotations()) {
                recordFramework(
                        annotation.annotationType(),
                        frameworks,
                        new LinkedHashSet<Class<?>>(),
                        0);
            }
        }
        // A declaration on the most-derived class shadows inherited methods
        // with the same public selector name, even when it is not a test.
        if (methodName != null && namedDeclarationFound) {
            return true;
        }
        for (Class<?> contract : type.getInterfaces()) {
            if (recordMethodFrameworks(
                    contract, methodName, frameworks, visitedTypes)
                    && methodName != null) {
                return true;
            }
        }
        boolean inherited = recordMethodFrameworks(
                type.getSuperclass(), methodName, frameworks, visitedTypes);
        return namedDeclarationFound || inherited;
    }

    private static void recordFramework(
            Class<?> annotationType,
            Set<String> frameworks,
            Set<Class<?>> visited,
            int depth) {
        if (!visited.add(annotationType) || depth > 8) {
            return;
        }
        String annotation = annotationType.getName();
        if ("org.junit.Test".equals(annotation)
                || "org.junit.runner.RunWith".equals(annotation)) {
            frameworks.add("junit4");
        }
        if (annotation.startsWith("org.junit.jupiter.api.")
                || annotation.startsWith("org.junit.jupiter.params.")) {
            frameworks.add("junit5");
        }
        if (annotation.startsWith("org.testng.annotations.")) {
            frameworks.add("testng");
        }
        if (annotation.startsWith("java.lang.annotation.")) {
            return;
        }
        for (java.lang.annotation.Annotation meta
                : annotationType.getAnnotations()) {
            recordFramework(
                    meta.annotationType(), frameworks, visited, depth + 1);
        }
    }

    private static Method findExecute(Class<?> launcherType, Class<?> requestType)
            throws NoSuchMethodException {
        for (Method method : launcherType.getMethods()) {
            if ("execute".equals(method.getName())
                    && method.getParameterTypes().length >= 1
                    && method.getParameterTypes()[0].isAssignableFrom(requestType)) {
                return method;
            }
        }
        throw new NoSuchMethodException("JUnit Platform Launcher.execute");
    }

    private static long number(Object target, String method) throws Exception {
        return ((Number) target.getClass().getMethod(method).invoke(target))
                .longValue();
    }

    private static long optionalNumber(Object target, String method)
            throws Exception {
        try {
            return number(target, method);
        } catch (NoSuchMethodException ignored) {
            return 0;
        }
    }

    private static long number(
            Class<?> api, Object target, String method) throws Exception {
        return ((Number) api.getMethod(method).invoke(target)).longValue();
    }

    private static Failure failure(String test, Throwable error) {
        StringWriter trace = new StringWriter();
        error.printStackTrace(new PrintWriter(trace));
        return new Failure(
                test,
                error.getClass().getName(),
                bounded(error.getMessage()),
                bounded(trace.toString())
        );
    }

    private static List<String> readSelectors(Path path) throws IOException {
        List<String> selectors = new ArrayList<String>();
        try (BufferedReader reader = Files.newBufferedReader(
                path, StandardCharsets.UTF_8)) {
            for (String line; (line = reader.readLine()) != null;) {
                String value = line.trim();
                if (!value.isEmpty()) {
                    Selector.parse(value);
                    selectors.add(value);
                }
            }
        }
        if (selectors.isEmpty() || selectors.size() > 64) {
            throw new IllegalArgumentException("Invalid test selector count.");
        }
        return selectors;
    }

    private static void configureProjectClasspath(Path path)
            throws IOException {
        List<String> entries = new ArrayList<String>();
        try (BufferedReader reader = Files.newBufferedReader(
                path, StandardCharsets.UTF_8)) {
            for (String line; (line = reader.readLine()) != null;) {
                if (line.isEmpty()) {
                    continue;
                }
                Path entry = Paths.get(line).toAbsolutePath().normalize();
                if (!Files.exists(entry)) {
                    throw new IOException("Test classpath entry is unavailable.");
                }
                entries.add(entry.toString());
                if (entries.size() > 4096) {
                    throw new IOException("Test classpath has too many entries.");
                }
            }
        }
        if (entries.isEmpty()) {
            throw new IOException("Test classpath is empty.");
        }
        System.setProperty("java.class.path", String.join(
                File.pathSeparator, entries));
    }

    private static Map<String, String> parse(String[] args) {
        Map<String, String> values = new LinkedHashMap<String, String>();
        for (int index = 0; index + 1 < args.length; index += 2) {
            if (!args[index].startsWith("--")) {
                return new LinkedHashMap<String, String>();
            }
            values.put(args[index].substring(2), args[index + 1]);
        }
        return values;
    }

    private static void sendInfrastructureFailure(
            String host,
            String portText,
            String token,
            String runId,
            Throwable error) throws IOException {
        if (host == null || portText == null || token == null || runId == null) {
            return;
        }
        error = rootCause(error);
        try (Socket socket = new Socket(
                InetAddress.getByName(host), Integer.parseInt(portText));
             BufferedWriter protocol = new BufferedWriter(
                     new OutputStreamWriter(
                             socket.getOutputStream(), StandardCharsets.UTF_8))) {
            emit(protocol, "{\"event\":\"infrastructure_failed\","
                    + "\"token\":" + json(token) + ","
                    + "\"test_run_id\":" + json(runId) + ","
                    + "\"error_type\":" + json(error.getClass().getName()) + ","
                    + "\"message\":" + json(bounded(error.getMessage())) + "}");
        }
    }

    private static Throwable rootCause(Throwable error) {
        Throwable current = error;
        Set<Throwable> seen = new LinkedHashSet<Throwable>();
        while (current != null && seen.add(current)) {
            Throwable next = current instanceof InvocationTargetException
                    ? ((InvocationTargetException) current).getTargetException()
                    : current.getCause();
            if (next == null || next == current) {
                return current;
            }
            current = next;
        }
        return current == null ? error : current;
    }

    private static String hello(String token, String runId) {
        return "{\"event\":\"runner_ready\",\"token\":" + json(token)
                + ",\"test_run_id\":" + json(runId) + "}";
    }

    private static String resultJson(
            String token,
            String runId,
            String framework,
            Result result) {
        StringBuilder out = new StringBuilder(2048);
        out.append("{\"event\":\"run_finished\",\"token\":")
                .append(json(token))
                .append(",\"test_run_id\":").append(json(runId))
                .append(",\"framework\":").append(json(framework))
                .append(",\"tests\":").append(result.tests)
                .append(",\"passed_count\":").append(result.passed)
                .append(",\"failed_count\":").append(result.failed)
                .append(",\"failed_test_count\":").append(result.failedTests)
                .append(",\"failed_container_count\":")
                .append(result.failedContainers)
                .append(",\"skipped_count\":").append(result.skipped)
                .append(",\"duration_ms\":").append(result.durationMs)
                .append(",\"passed\":").append(result.failed == 0)
                .append(",\"failures\":[");
        for (int index = 0; index < result.failures.size(); index++) {
            if (index > 0) {
                out.append(',');
            }
            Failure failure = result.failures.get(index);
            out.append("{\"test\":").append(json(failure.test))
                    .append(",\"exception\":").append(json(failure.exception))
                    .append(",\"message\":").append(json(failure.message))
                    .append(",\"trace\":").append(json(failure.trace))
                    .append('}');
        }
        return out.append("]}").toString();
    }

    private static void emit(BufferedWriter protocol, String frame)
            throws IOException {
        protocol.write(frame);
        protocol.newLine();
        protocol.flush();
    }

    private static String bounded(String value) {
        if (value == null) {
            return "";
        }
        return value.length() <= MAX_TEXT
                ? value : value.substring(0, MAX_TEXT);
    }

    private static String json(String value) {
        StringBuilder result = new StringBuilder("\"");
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            switch (character) {
                case '\\': result.append("\\\\"); break;
                case '"': result.append("\\\""); break;
                case '\n': result.append("\\n"); break;
                case '\r': result.append("\\r"); break;
                case '\t': result.append("\\t"); break;
                default:
                    if (character < 0x20) {
                        result.append(String.format("\\u%04x", (int) character));
                    } else {
                        result.append(character);
                    }
            }
        }
        return result.append('"').toString();
    }

    private static final class Selector {
        final String className;
        final String methodName;

        Selector(String className, String methodName) {
            this.className = className;
            this.methodName = methodName;
        }

        static Selector parse(String raw) {
            int marker = raw.indexOf('#');
            String className = marker < 0 ? raw : raw.substring(0, marker);
            String methodName = marker < 0 ? null : raw.substring(marker + 1);
            if (!className.matches("[A-Za-z_$][A-Za-z0-9_$.]*")
                    || (methodName != null
                    && !methodName.matches("[A-Za-z_$][A-Za-z0-9_$]*"))) {
                throw new IllegalArgumentException("Invalid test selector.");
            }
            return new Selector(className, methodName);
        }
    }

    private static final class Result {
        String framework;
        long tests;
        long passed;
        long failed;
        long failedTests;
        long failedContainers;
        long skipped;
        long durationMs;
        final List<Failure> failures = new ArrayList<Failure>();

        void addFailure(Failure failure) {
            if (failures.size() < MAX_FAILURES) {
                failures.add(failure);
            }
        }

        void merge(Result other) {
            tests += other.tests;
            passed += other.passed;
            failed += other.failed;
            failedTests += other.failedTests;
            failedContainers += other.failedContainers;
            skipped += other.skipped;
            durationMs += other.durationMs;
            for (Failure failure : other.failures) {
                addFailure(failure);
            }
        }
    }

    private static final class Failure {
        final String test;
        final String exception;
        final String message;
        final String trace;

        Failure(String test, String exception, String message, String trace) {
            this.test = test;
            this.exception = exception;
            this.message = message;
            this.trace = trace;
        }
    }
}
