package net.jolink.runtime.jdt;

import java.util.List;

import org.eclipse.core.internal.events.BuildCommand;
import org.eclipse.core.internal.events.BuildManager;
import org.eclipse.core.internal.events.BuilderPersistentInfo;
import org.eclipse.core.internal.resources.Workspace;
import org.eclipse.core.resources.ICommand;
import org.eclipse.core.resources.IProject;
import org.eclipse.core.runtime.CoreException;

/** Preserve restored builder trees when a Worker exits without running a build. */
final class RestoredBuilderState {
    static void normalizeConfigurationNames(IProject project) throws CoreException {
        BuildManager manager = ((Workspace) project.getWorkspace()).getBuildManager();
        List<BuilderPersistentInfo> infos = manager.getBuildersPersistentInfo(project);
        if (infos == null) return;

        // Eclipse Resources 4.19 saves null as the active configuration name
        // (normally ""). On reopen, createBuildersPersistentInfo looks up an
        // uninstantiated, non-configurable builder using null instead. Without
        // normalization that lookup misses and the existing tree is not saved.
        // Keep the tree itself unchanged; configuration-aware builders retain
        // their individual configuration names.
        for (ICommand raw : project.getDescription().getBuildSpec()) {
            BuildCommand command = (BuildCommand) raw;
            if (command.supportsConfigs()) continue;
            for (BuilderPersistentInfo info : infos) {
                if (command.getBuilderName().equals(info.getBuilderName())) {
                    info.setConfigName(null);
                }
            }
        }
    }
}
