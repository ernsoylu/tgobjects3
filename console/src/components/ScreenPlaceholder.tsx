/**
 * Minimal placeholder shown by each screen stub until task 44 implements the
 * real UI. Keeps the shell navigable without pretending functionality exists.
 */
export function ScreenPlaceholder(props: { title: string; description: string }): JSX.Element {
  return (
    <section className="screen">
      <h1 className="screen-title">{props.title}</h1>
      <p className="screen-description">{props.description}</p>
      <p className="screen-note">Implemented in task 44.</p>
    </section>
  );
}
