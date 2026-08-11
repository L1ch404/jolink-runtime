package example;

import java.util.List;

public final class Java9ApiLeak {
    public List<String> values() {
        return List.of("not-java-8");
    }
}
