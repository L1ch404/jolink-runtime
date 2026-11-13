package net.jolink.runtime.jdt;

import java.io.InputStream;
import java.util.concurrent.atomic.AtomicBoolean;
import org.osgi.framework.Bundle;
import org.osgi.framework.BundleActivator;
import org.osgi.framework.BundleContext;
import org.osgi.framework.ServiceRegistration;
import org.osgi.framework.hooks.weaving.WeavingHook;

/** Applies the locked JDT saved-state fix once, before the builder loads State. */
public final class StatePatchActivator implements BundleActivator {
    private static final String STATE = "org.eclipse.jdt.internal.core.builder.State";

    @Override
    public void start(BundleContext context) throws Exception {
        byte[] replacement;
        try (InputStream stream = context.getBundle().getResource(
                "META-INF/jolink/patches/" + STATE.replace('.', '/') + ".class").openStream()) {
            replacement = stream.readAllBytes();
        }
        AtomicBoolean applied = new AtomicBoolean();
        ServiceRegistration<WeavingHook> registration = context.registerService(
                WeavingHook.class, wovenClass -> {
                    if (STATE.equals(wovenClass.getClassName()) && "org.eclipse.jdt.core".equals(
                            wovenClass.getBundleWiring().getBundle().getSymbolicName())) {
                        wovenClass.setBytes(replacement);
                        applied.set(true);
                    }
                }, null);
        try {
            for (Bundle bundle : context.getBundles()) {
                if ("org.eclipse.jdt.core".equals(bundle.getSymbolicName())) {
                    bundle.loadClass(STATE);
                    if (!applied.get()) {
                        throw new IllegalStateException("JDT State was loaded before its persistence fix.");
                    }
                    return;
                }
            }
            throw new IllegalStateException("JDT Core is unavailable.");
        } finally {
            // State is now defined in JDT's classloader; no later class needs a hook.
            registration.unregister();
        }
    }

    @Override
    public void stop(BundleContext context) {
        // No registration or background work survives start().
    }
}
