package example;

public class Legacy {
    public static class Inner {
        public int value() {
            return 1;
        }
    }

    public static class GenericBase<T> {
        public T value() {
            return null;
        }
    }

    public static class Bridge extends GenericBase<String> {
        @Override
        public String value() {
            return "bridge";
        }
    }

    public Runnable anonymousTask() {
        return new Runnable() {
            @Override
            public void run() {
                // fixture behavior is intentionally empty
            }
        };
    }

    public int localValue() {
        class Local {
            int value() {
                return 2;
            }
        }
        return new Local().value();
    }
}
