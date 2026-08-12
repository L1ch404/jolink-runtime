package example;

public class Legacy {
    public static class Inner {
        public int value() {
            return 1;
        }
    }

    public Runnable task() {
        return new Runnable() {
            @Override
            public void run() {
                // deterministic class-family fixture
            }
        };
    }
}
