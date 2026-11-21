// CC0 1.0: this sample may be used for any purpose.
#set document(title: "pdf2md-local sample", author: "pdf2md-local")
#set page(paper: "a5", margin: 1.6cm, numbering: "1")
#set heading(numbering: "1.1")
#set text(size: 10pt)
= Calculus essentials
This short sample is written for pdf2md-local and released under CC0. It has numbered chapters, equations, a table and a figure, so you can check an installation in a minute.

== The derivative
For a function $f$, the derivative at $x$ is
$ f'(x) = lim_(h -> 0) (f(x+h) - f(x)) / h . $
For example, if $f(x) = x^2$ then $f'(x) = 2x$, and the Gaussian integral is
$ integral_(-infinity)^(infinity) e^(-x^2) dif x = sqrt(pi) . $

== A table of derivatives
#align(center, table(columns: 3, inset: 5pt,
  [*Function*], [*Derivative*], [*Domain*],
  [$x^n$], [$n x^(n-1)$], [$x > 0$],
  [$sin x$], [$cos x$], [all $x$],
  [$e^x$], [$e^x$], [all $x$],
))

= Linear algebra
== Matrices
A matrix with entries $a_(i j)$ acts on a vector $bold(v)$:
$ A bold(v) = mat(a, b; c, d) vec(x, y) = vec(a x + b y, c x + d y) . $
The determinant of a two by two matrix is $det A = a d - b c$.

== A figure
#align(center, rect(width: 6cm, height: 3cm, stroke: 0.6pt, inset: 0pt)[
  #place(line(start: (0.3cm, 2.5cm), end: (5.7cm, 0.4cm), stroke: 1.2pt))
  #place(line(start: (0.3cm, 2.7cm), end: (5.7cm, 2.7cm), stroke: 0.4pt))
  #place(dx: 2.4cm, dy: 1.2cm, circle(radius: 2pt, fill: black))
])
#align(center)[_Figure 1._ A line crossing an axis.]
