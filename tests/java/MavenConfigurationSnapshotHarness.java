package io.jolink.maven;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.model.building.ModelProcessor;
import org.apache.maven.plugin.AbstractMojo;
import org.apache.maven.plugin.MojoExecutionException;

/** A real Maven Mojo that observes the bundled Probe's native snapshot helper. */
public final class MavenConfigurationSnapshotHarness extends AbstractMojo {
    public MavenSession session;
    public ModelProcessor modelProcessor;
    public String destination;
    public String gate;

    @Override
    public void execute() throws MojoExecutionException {
        try {
            ConfigurationSnapshot snapshot = Boolean.getBoolean("snapshot.beforeGate")
                    ? ConfigurationSnapshot.capture(session.getProjects(), modelProcessor) : null;
            if (gate != null && !gate.isEmpty()) {
                Path directory = Paths.get(gate);
                Files.createDirectories(directory);
                Files.write(directory.resolve("entered"), "models-already-built".getBytes(StandardCharsets.UTF_8));
                long deadline = System.nanoTime() + 30_000_000_000L;
                while (!Files.exists(directory.resolve("release"))) {
                    if (System.nanoTime() >= deadline) throw new IllegalStateException("test gate timed out");
                    Thread.sleep(20);
                }
            }
            if (snapshot == null) snapshot = ConfigurationSnapshot.capture(session.getProjects(), modelProcessor);
            StringBuilder json = new StringBuilder("{\"native_session\":true");
            snapshot.appendTo(json);
            json.append('}');
            Files.write(Paths.get(destination), json.toString().getBytes(StandardCharsets.UTF_8));
        } catch (Exception error) {
            throw new MojoExecutionException("Configuration snapshot regression failed", error);
        }
    }
}
