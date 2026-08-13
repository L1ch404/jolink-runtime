package example;

public class LombokConsumer {
    public String describe() {
        LombokModel model = LombokModel.builder()
                .name("phase-1b")
                .count(2)
                .build();
        model.setCount(model.getCount() + 1);
        return model.getName() + ":" + model.getCount()
                + ":" + model.normalize(" ok ");
    }
}
