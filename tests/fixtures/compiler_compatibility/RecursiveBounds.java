import java.util.List;

class RecursiveBounds {
  static <T> T identity(T value) { return value; }

  Iterable<Enum<?>> convert(List<Enum<? extends Enum<?>>> value) {
    return identity(value);
  }
}
