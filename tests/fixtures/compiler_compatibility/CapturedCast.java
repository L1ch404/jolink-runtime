import java.util.Map;

abstract class CapturedCast<B> implements Map<Class<? extends B>, B> {
  static <B, S extends B> CapturedCast<B> copy(
      Map<? extends Class<? extends S>, ? extends S> value) {
    return (CapturedCast<B>) value;
  }
}
