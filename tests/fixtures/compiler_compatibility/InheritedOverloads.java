import java.io.Serializable;
import java.util.AbstractList;
import java.util.RandomAccess;

class InheritedOverloads {
  interface Future<V> extends java.util.concurrent.Future<V> {}
  static abstract class BaseFuture<V> implements Future<V> {}
  static abstract class MiddleFuture<V> extends BaseFuture<V> {}
  static abstract class FluentFuture<V> extends MiddleFuture<V> {}
  static abstract class MyList<E> extends AbstractList<E> implements RandomAccess, Serializable {}
  static class Combiner<V> {}
  static <V> Combiner<V> succeed(Future<? extends V>... inputs) { return null; }
  static <V> Combiner<V> succeed(Iterable<? extends Future<? extends V>> inputs) { return null; }
  static <V> Combiner<V> complete(Future<? extends V>... inputs) { return null; }
  static <V> Combiner<V> complete(Iterable<? extends Future<? extends V>> inputs) { return null; }
  static MyList<FluentFuture<?>> inputs() { return null; }

  Combiner<Object> combine(boolean flag) {
    return flag ? succeed(inputs()) : complete(inputs());
  }
}
