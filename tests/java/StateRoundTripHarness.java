package org.eclipse.jdt.internal.core.builder;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.function.Consumer;
import org.eclipse.core.resources.IProject;
import org.eclipse.core.resources.IWorkspace;
import org.eclipse.jdt.internal.compiler.env.IUpdatableModule;
import org.eclipse.jdt.internal.compiler.env.IUpdatableModule.AddExports;
import org.eclipse.jdt.internal.compiler.env.IUpdatableModule.AddReads;
import org.eclipse.jdt.internal.compiler.env.IUpdatableModule.UpdateKind;

/** Runs the shipped native State codec without a simulated Python serializer. */
public final class StateRoundTripHarness {
    private static final IWorkspace WORKSPACE = (IWorkspace) Proxy.newProxyInstance(
            IWorkspace.class.getClassLoader(), new Class<?>[] {IWorkspace.class},
            (object, method, args) -> null);
    private static final IProject PROJECT = (IProject) Proxy.newProxyInstance(
            IProject.class.getClassLoader(), new Class<?>[] {IProject.class},
            (object, method, args) -> method.getName().equals("getWorkspace") ? WORKSPACE : "codec");

    private static ClasspathLocation location(String name, Set<String> limits, String... reads) {
        ClasspathLocation value = ClasspathLocation.forLibrary(
                "/codec/" + name + ".jar", 123L, null, null, true, null);
        value.limitModuleNames = limits;
        if (reads.length != 0) {
            value.updates = new IUpdatableModule.UpdatesByKind();
            for (String target : reads)
                value.updates.getList(UpdateKind.MODULE, true).add(new AddReads(target.toCharArray()));
        }
        return value;
    }

    private static List<String> reads(ClasspathLocation value) {
        List<String> result = new ArrayList<>();
        if (value.updates != null) {
            for (Consumer<IUpdatableModule> entry : value.updates.getList(UpdateKind.MODULE, false))
                result.add(new String(((AddReads) entry).getTarget()));
        }
        return result;
    }

    private static ClasspathLocation[] roundTrip(ClasspathLocation... entries) throws Exception {
        Method write = State.class.getDeclaredMethod("writeBinaryLocations", CompressedWriter.class,
                ClasspathLocation[].class, ClasspathMultiDirectory[].class);
        Method read = State.class.getDeclaredMethod("readBinaryLocations", IProject.class,
                CompressedReader.class, ClasspathMultiDirectory[].class, ArrayList.class);
        write.setAccessible(true);
        read.setAccessible(true);
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        DataOutputStream output = new DataOutputStream(bytes);
        CompressedWriter writer = new CompressedWriter(output);
        write.invoke(new State(), writer, entries, new ClasspathMultiDirectory[0]);
        // A distinct trailing record catches under-reading, including N>1 AddReads.
        writer.writeInt(0x1a2b3c4d);
        DataInputStream input = new DataInputStream(new ByteArrayInputStream(bytes.toByteArray()));
        CompressedReader reader = new CompressedReader(input);
        ClasspathLocation[] restored = (ClasspathLocation[]) read.invoke(
                null, PROJECT, reader, new ClasspathMultiDirectory[0], null);
        check(reader.readInt() == 0x1a2b3c4d && input.available() == 0, "stream alignment");
        return restored;
    }

    private static void check(boolean condition, String detail) {
        if (!condition) throw new AssertionError(detail);
    }

    private static void moduleLimits() throws Exception {
        ClasspathLocation[] before = {
            location("null", null),
            location("empty", ClasspathJrt.NO_LIMIT_MODULES),
            location("bounded", new LinkedHashSet<>(Arrays.asList("java.base", "java.sql")))
        };
        ClasspathLocation[] after = roundTrip(before);
        check(after[0].limitModuleNames == null, "null module limits preserved");
        check(after[1].limitModuleNames == ClasspathJrt.NO_LIMIT_MODULES,
                "empty module limits retain JRT sentinel identity, not just Set.equals");
        check(before[2].limitModuleNames.equals(after[2].limitModuleNames), "bounded module limits");
        for (int i = 0; i < before.length; i++)
            check(before[i].equals(after[i]), "classpath equality for module limits " + i);
    }

    private static void moduleUpdates() throws Exception {
        ClasspathLocation[] before = {
            location("none", null),
            location("single", ClasspathJrt.NO_LIMIT_MODULES, "ALL-UNNAMED"),
            location("duplicate", ClasspathJrt.NO_LIMIT_MODULES, "ALL-UNNAMED", "ALL-UNNAMED"),
            location("distinct", new LinkedHashSet<>(Arrays.asList("java.base", "java.sql")),
                    "ALL-UNNAMED", "java.sql", "java.logging"),
            location("trailing", null, "java.xml")
        };
        before[3].patchModuleName = "example.app";
        before[3].updates.getList(UpdateKind.PACKAGE, true).add(
                new AddExports("example.internal".toCharArray(),
                        new char[][] {"friend.one".toCharArray(), "friend.two".toCharArray()}));
        ClasspathLocation[] after = roundTrip(before);
        for (int i = 0; i < before.length; i++) {
            check(reads(before[i]).equals(reads(after[i])), "read edges and duplicates " + i);
            check(before[i].equals(after[i]), "classpath equality for module updates " + i);
        }
        check("example.app".equals(after[3].patchModuleName), "patch module name preserved");
        AddExports exports = (AddExports) after[3].updates.getList(UpdateKind.PACKAGE, false).get(0);
        check("example.internal".equals(new String(exports.getName())), "exported package preserved");
        check(Arrays.deepEquals(exports.getTargetModules(),
                new char[][] {"friend.one".toCharArray(), "friend.two".toCharArray()}), "export targets preserved");
    }

    private static void previousFormatRejected() throws Exception {
        Method read = State.class.getDeclaredMethod("read", IProject.class, DataInputStream.class);
        read.setAccessible(true);
        // 0x27 is the actual previous upstream format, whose zero limit count
        // conflates null with the JRT no-limit sentinel and cannot be recovered.
        Object restored = read.invoke(null, PROJECT,
                new DataInputStream(new ByteArrayInputStream(new byte[] {0x27})));
        check(restored == null, "previous ambiguous format rejected before reading fields");
    }

    public static void main(String[] arguments) throws Exception {
        moduleLimits();
        moduleUpdates();
        previousFormatRejected();
        System.out.println("STATE_ROUNDTRIP_OK: limits, reads, patch, exports, stream alignment, previous format");
    }
}
