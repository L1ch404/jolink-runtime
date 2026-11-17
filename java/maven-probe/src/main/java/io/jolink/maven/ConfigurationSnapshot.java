package io.jolink.maven;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.IOException;
import java.io.StringWriter;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;
import org.apache.maven.model.Model;
import org.apache.maven.model.building.FileModelSource;
import org.apache.maven.model.building.ModelProcessor;
import org.apache.maven.model.io.ModelParseException;
import org.apache.maven.model.io.ModelReader;
import org.apache.maven.model.io.xpp3.MavenXpp3Writer;
import org.apache.maven.project.MavenProject;

/** Bind Maven's retained raw models to bytes before any preparation goals run. */
final class ConfigurationSnapshot {
    final Map<String, String> stamps = new LinkedHashMap<>();
    final Set<String> changes = new LinkedHashSet<>();

    static ConfigurationSnapshot capture(Iterable<MavenProject> projects, ModelProcessor reader) throws IOException {
        ConfigurationSnapshot result = new ConfigurationSnapshot();
        Map<String, String> currentModels = new LinkedHashMap<>();
        Set<MavenProject> seen = Collections.newSetFromMap(new IdentityHashMap<MavenProject, Boolean>());
        for (MavenProject project : projects) {
            for (MavenProject item = project; item != null && seen.add(item); item = item.getParent()) {
                File file = item.getFile();
                if (file == null) continue; // Maven's synthetic super model has no local POM.
                String path = file.getCanonicalPath();
                if (!result.stamps.containsKey(path)) {
                    if (!file.isFile()) {
                        result.stamps.put(path, "missing");
                        result.changes.add(path);
                    } else {
                        // Parse and hash the same bytes. A later filesystem read
                        // must not relabel an older parsed model with newer content.
                        byte[] bytes = Files.readAllBytes(file.toPath());
                        result.stamps.put(path, sha256(bytes));
                        try {
                            Map<String, Object> options = new LinkedHashMap<>();
                            options.put(ModelReader.IS_STRICT, Boolean.FALSE);
                            options.put(ModelProcessor.SOURCE, new FileModelSource(file));
                            currentModels.put(path, canonical(reader.read(new ByteArrayInputStream(bytes), options)));
                        } catch (ModelParseException malformed) {
                            result.changes.add(path);
                        }
                    }
                }
                // Compare every retained model even if several projects share
                // this POM path; parent instances need not be the same object.
                Model original = item.getOriginalModel();
                if (original == null || !canonical(original).equals(currentModels.get(path)))
                    result.changes.add(path);
            }
        }
        return result;
    }

    void appendTo(StringBuilder json) {
        json.append(",\"configurationStamps\":{");
        boolean comma = false;
        for (Map.Entry<String, String> entry : stamps.entrySet()) {
            ExportBuildWorldMojo.field(json, entry.getKey(), entry.getValue(), comma);
            comma = true;
        }
        json.append('}');
        ExportBuildWorldMojo.stringList(json, "configurationChanges", new ArrayList<>(changes), true);
    }

    private static String canonical(Model model) throws IOException {
        StringWriter writer = new StringWriter();
        new MavenXpp3Writer().write(writer, model);
        return writer.toString();
    }

    private static String sha256(byte[] bytes) {
        try {
            StringBuilder result = new StringBuilder(64);
            for (byte value : MessageDigest.getInstance("SHA-256").digest(bytes))
                result.append(String.format("%02x", value & 0xff));
            return result.toString();
        } catch (NoSuchAlgorithmException impossible) {
            throw new IllegalStateException(impossible);
        }
    }
}
