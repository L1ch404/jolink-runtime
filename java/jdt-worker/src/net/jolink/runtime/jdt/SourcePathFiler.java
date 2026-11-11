package net.jolink.runtime.jdt;

import java.io.FileNotFoundException;
import java.io.FilterInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.Reader;
import java.io.Writer;
import java.lang.reflect.Field;
import java.net.URI;
import java.net.URISyntaxException;
import java.nio.charset.Charset;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.NoSuchFileException;
import java.nio.file.NotDirectoryException;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.TimeUnit;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

import javax.annotation.processing.Filer;
import javax.annotation.processing.FilerException;
import javax.lang.model.element.Element;
import javax.tools.FileObject;
import javax.tools.JavaFileManager.Location;
import javax.tools.JavaFileObject;
import javax.tools.StandardLocation;

import org.eclipse.core.resources.IFolder;
import org.eclipse.jdt.core.IJavaProject;
import org.eclipse.jdt.internal.compiler.apt.dispatch.BaseProcessingEnvImpl;

/** Add source-path reads without replacing Eclipse's processing environment or outputs. */
final class SourcePathFiler implements Filer {
    private static final Map<String, List<Path>> roots = new LinkedHashMap<>();
    private static final Map<String, Map<Path, long[]>> observations = new LinkedHashMap<>();

    private final Filer delegate;
    private final String project;
    private final List<Path> sourceRoots;
    private final Charset charset;

    private SourcePathFiler(Filer delegate, String project, List<Path> sourceRoots, Charset charset) {
        this.delegate = delegate;
        this.project = project;
        this.sourceRoots = sourceRoots;
        this.charset = charset;
    }

    static synchronized String configure(Path mapping) throws IOException {
        roots.clear();
        for (String line : Files.readAllLines(mapping, StandardCharsets.UTF_8)) {
            if (line.isEmpty()) continue;
            String[] parts = line.split("\t", -1);
            String project = decode(parts[0]);
            List<Path> configured = roots.computeIfAbsent(project, key -> new ArrayList<>());
            if (parts.length > 1) configured.add(Paths.get(decode(parts[1])).toAbsolutePath().normalize());
        }
        return "{\"ok\":true,\"status\":\"apt_source_path_configured\"}";
    }

    static void install(IJavaProject project, BaseProcessingEnvImpl environment, boolean test) {
        final List<Path> configured;
        synchronized (SourcePathFiler.class) {
            List<Path> paths = roots.get(project.getElementName());
            if (paths == null) return; // Legacy/direct Worker clients retain Eclipse's behavior.
            configured = List.copyOf(paths);
        }
        try {
            // Worker initialization sets the source folder's encoding, not the
            // workspace/project default. Split main/test projects each use src.
            IFolder source = project.getProject().getFolder("test-src");
            if (!test || !source.exists()) source = project.getProject().getFolder("src");
            Charset charset = Charset.forName(source.getDefaultCharset(true));
            Field filer = BaseProcessingEnvImpl.class.getDeclaredField("_filer");
            filer.setAccessible(true);
            filer.set(environment, new SourcePathFiler(environment.getFiler(),
                    project.getElementName(), configured, charset));
        } catch (Exception error) {
            throw new IllegalArgumentException("Cannot configure APT source-path resource reads.", error);
        }
    }

    static synchronized void beginBuild() {
        observations.clear();
    }

    static synchronized String inputs() {
        StringBuilder result = new StringBuilder("{\"ok\":true,\"projects\":{");
        boolean firstProject = true;
        for (Map.Entry<String, Map<Path, long[]>> entry : observations.entrySet()) {
            if (!firstProject) result.append(',');
            firstProject = false;
            result.append(WorkerApplication.json(entry.getKey())).append(":{");
            boolean firstFile = true;
            for (Map.Entry<Path, long[]> file : entry.getValue().entrySet()) {
                if (!firstFile) result.append(',');
                firstFile = false;
                result.append(WorkerApplication.json(file.getKey().toString())).append(':');
                long[] stamp = file.getValue();
                if (stamp == null) result.append("null");
                else result.append('[').append(stamp[0]).append(',').append(stamp[1]).append(']');
            }
            result.append('}');
        }
        return result.append("}}").toString();
    }

    private static String decode(String value) {
        return new String(Base64.getDecoder().decode(value), StandardCharsets.UTF_8);
    }

    private boolean observe(Path file) throws IOException {
        long[] stamp = null;
        try {
            BasicFileAttributes metadata = Files.readAttributes(file, BasicFileAttributes.class);
            if (metadata.isRegularFile()) stamp = new long[] {
                    metadata.lastModifiedTime().to(TimeUnit.NANOSECONDS), metadata.size()};
        } catch (NoSuchFileException | NotDirectoryException missing) {
            // Missing earlier roots matter too: later creation can shadow a hit.
        }
        synchronized (SourcePathFiler.class) {
            Map<Path, long[]> files = observations.computeIfAbsent(project, key -> new LinkedHashMap<>());
            // putIfAbsent would overwrite a first observation whose value is null.
            if (!files.containsKey(file)) files.put(file, stamp);
        }
        return stamp != null;
    }

    @Override
    public FileObject getResource(Location location, CharSequence pkg, CharSequence relativeName) throws IOException {
        if (location != StandardLocation.SOURCE_PATH) return delegate.getResource(location, pkg, relativeName);
        String packageName = Objects.requireNonNull(pkg, "Package").toString();
        String name = Objects.requireNonNull(relativeName, "Relative name").toString();
        if (packageName.indexOf('/') >= 0) throw new FilerException("SOURCE_PATH does not accept a module-qualified package");
        validateRelativeName(name);
        if (packageName.indexOf('\\') >= 0 || packageName.startsWith(".") || packageName.endsWith(".")
                || packageName.contains("..")) throw new IllegalArgumentException("Invalid package: " + packageName);
        Path relative = Paths.get(packageName.replace('.', '/')).resolve(name);
        String entryName = relative.toString().replace('\\', '/');
        for (Path root : sourceRoots) {
            // Watch the outer file even when this entry is absent. A replacement
            // archive can introduce a higher-priority hit on the next build.
            if (observe(root)) {
                try (ZipFile archive = new ZipFile(root.toFile())) {
                    ZipEntry entry = archive.getEntry(entryName);
                    if (entry != null && !entry.isDirectory()) return new ArchiveFile(root, entryName, charset);
                }
            } else {
                Path file = root.resolve(relative).normalize();
                if (observe(file)) return new SourceFile(file, charset);
            }
        }
        throw new FileNotFoundException("SOURCE_PATH resource not found: " + relative);
    }

    private static void validateRelativeName(String name) {
        int slash = name.indexOf('/'), colon = name.indexOf(':');
        if (name.isEmpty() || name.indexOf('\\') >= 0 || name.startsWith("/")
                || (colon >= 0 && (slash < 0 || colon < slash)))
            throw new IllegalArgumentException("Resource name must be relative: " + name);
        for (String part : name.split("/", -1)) {
            if (part.isEmpty() || part.equals(".") || part.equals(".."))
                throw new IllegalArgumentException("Invalid relative resource name: " + name);
        }
    }

    @Override public JavaFileObject createSourceFile(CharSequence name, Element... origins) throws IOException {
        return delegate.createSourceFile(name, origins);
    }
    @Override public JavaFileObject createClassFile(CharSequence name, Element... origins) throws IOException {
        return delegate.createClassFile(name, origins);
    }
    @Override public FileObject createResource(Location location, CharSequence pkg, CharSequence name,
            Element... origins) throws IOException {
        return delegate.createResource(location, pkg, name, origins);
    }

    private abstract static class ReadOnlyResource implements FileObject {
        private final Charset charset;

        ReadOnlyResource(Charset charset) {
            this.charset = charset;
        }
        @Override public OutputStream openOutputStream() { throw new UnsupportedOperationException("Read-only resource"); }
        @Override public Writer openWriter() { throw new UnsupportedOperationException("Read-only resource"); }
        @Override public boolean delete() { return false; }
        @Override public Reader openReader(boolean ignoreEncodingErrors) throws IOException {
            CodingErrorAction action = ignoreEncodingErrors ? CodingErrorAction.REPLACE : CodingErrorAction.REPORT;
            var decoder = charset.newDecoder().onMalformedInput(action).onUnmappableCharacter(action);
            return new InputStreamReader(openInputStream(), decoder);
        }
        @Override public CharSequence getCharContent(boolean ignoreEncodingErrors) throws IOException {
            try (Reader reader = openReader(ignoreEncodingErrors)) {
                StringBuilder text = new StringBuilder();
                char[] buffer = new char[4096];
                for (int read; (read = reader.read(buffer)) >= 0;) text.append(buffer, 0, read);
                return text;
            }
        }
    }

    private static final class SourceFile extends ReadOnlyResource {
        private final Path file;

        SourceFile(Path file, Charset charset) {
            super(charset);
            this.file = file;
        }
        @Override public URI toUri() { return file.toUri(); }
        @Override public String getName() { return file.toString(); }
        @Override public InputStream openInputStream() throws IOException { return Files.newInputStream(file); }
        @Override public long getLastModified() {
            try { return Files.getLastModifiedTime(file).toMillis(); }
            catch (IOException error) { return 0L; }
        }
    }

    private static final class ArchiveFile extends ReadOnlyResource {
        private final Path file;
        private final String entryName;

        ArchiveFile(Path file, String entryName, Charset charset) {
            super(charset);
            this.file = file;
            this.entryName = entryName;
        }
        @Override public URI toUri() {
            try {
                String entry = new URI(null, null, "/" + entryName, null).getRawPath().substring(1);
                return URI.create("jar:" + file.toUri().toASCIIString().replace("!", "%21") + "!/" + entry);
            } catch (URISyntaxException invalid) {
                throw new IllegalArgumentException("Invalid archive resource name: " + entryName, invalid);
            }
        }
        @Override public String getName() { return file + "!/" + entryName; }
        @Override public InputStream openInputStream() throws IOException {
            ZipFile archive = new ZipFile(file.toFile());
            try {
                ZipEntry entry = archive.getEntry(entryName);
                if (entry == null || entry.isDirectory()) throw new FileNotFoundException(getName());
                return new FilterInputStream(archive.getInputStream(entry)) {
                    @Override public void close() throws IOException {
                        try { super.close(); }
                        finally { archive.close(); }
                    }
                };
            } catch (IOException | RuntimeException | Error failure) {
                try { archive.close(); }
                catch (IOException close) { failure.addSuppressed(close); }
                throw failure;
            }
        }
        @Override public long getLastModified() {
            try (ZipFile archive = new ZipFile(file.toFile())) {
                ZipEntry entry = archive.getEntry(entryName);
                return entry == null || entry.getTime() < 0 ? 0L : entry.getTime();
            } catch (IOException error) { return 0L; }
        }
    }
}
