import java.util.List;

class ConditionalOverload {
  interface Future<V> {}
  interface FluentFuture<V> extends Future<V> {}
  static class Combiner<V> {}
  static <V> Combiner<V> succeed(Future<? extends V>... inputs) { return null; }
  static <V> Combiner<V> succeed(Iterable<? extends Future<? extends V>> inputs) { return null; }
  static <V> Combiner<V> complete(Future<? extends V>... inputs) { return null; }
  static <V> Combiner<V> complete(Iterable<? extends Future<? extends V>> inputs) { return null; }
  static List<FluentFuture<?>> inputs() { return null; }

  Combiner<Object> combine(boolean flag) {
    return flag ? succeed(inputs()) : complete(inputs());
  }
}
