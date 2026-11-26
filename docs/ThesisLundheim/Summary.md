# Mesoscopic Tensorial Modelling of Crystal Plasticity

Elias Lundheim, *Mesoscopic Tensorial Modelling of Crystal Plasticity: Mesh
Reconnection, Elastoplastic Decomposition and Plastic Avalanches* (2026).
[Original thesis](PhD_Thesis_Lundheim.pdf) ·
[Searchable plain text](PhD_Thesis_Lundheim.txt)

Page references below use the thesis's printed numbering. Links open at the
corresponding PDF page. The text extraction marks PDF page numbers and retains
the page layout. Use the PDF for equation typography, figures, and mathematical
symbols that do not survive text extraction.

## Lattice geometry and strain

| Quantity | Definition and interpretation |
| --- | --- |
| $A=(\mathbf a_1,\mathbf a_2)$ | The basis vectors of the current crystal lattice, expressed in spatial Cartesian coordinates. The columns carry spatial directions and lengths. |
| $G=A^\mathsf T A$ | The lattice Gram matrix contains basis lengths and mutual angles. Rigid rotation, reflection, and translation leave it unchanged. A primitive basis change $A\mapsto AM$ with $M\in GL(2,\mathbb Z)$ sends $G\mapsto M^\mathsf TGM$. |
| $F$, $C=F^\mathsf TF$ | The deformation gradient and right Cauchy–Green tensor are relative to a material reference. If $A=FQ$ for the specified reference lattice basis $Q$, the corresponding Gram matrix is $Q^\mathsf TCQ$. |
| $G^{(e)}=(A^{(e)})^\mathsf TA^{(e)}$ | The mesh-edge Gram matrix uses the two shortest edges of the current triangle, directed from their common vertex. It describes element geometry for the reconnection criterion. |

[Chapter 2 §§2.1–2.6, pp. 31–36](PhD_Thesis_Lundheim.pdf#page=42) ·
[Chapter 3 §2.1, pp. 67–70](PhD_Thesis_Lundheim.pdf#page=78)

- Lagrange reduction selects a short lattice basis with
  $0\le 2G_{12}\le G_{11}\le G_{22}$. The normalized square and triangular
  representatives have equal basis lengths and mutual angles of 90° and 60°,
  respectively. Dividing a two-dimensional metric by $\sqrt{\det G}$
  isolates its shape for projection onto the Poincaré disk. The mutual angle
  describes lattice shape. The polar rotation of $F_e$ describes spatial
  orientation relative to the chosen reference.
  [Chapter 2 §§2.6–2.8, pp. 35–42](PhD_Thesis_Lundheim.pdf#page=46) ·
  [Chapter 4 §4.2, pp. 115–117](PhD_Thesis_Lundheim.pdf#page=126)
- An undeformed sample has $C=I$ in its own material reference. In
  normalized square-reference coordinates, the constitutive argument is
  $C_{\rm sq}=H^\mathsf TCH$. The triangular reference map $H$ has unit
  determinant and gives $C_{\rm sq}=H^\mathsf TH$ at rest. This retains
  the triangular shape in the common reference.
  [Appendix B.3, Eqs. (B.22)–(B.26), pp. 178–179](PhD_Thesis_Lundheim.pdf#page=189)

## Constitutive model and discretization

- The two-dimensional Mesoscopic Tensorial Model uses a nonconvex
  lattice-invariant energy and piecewise affine finite elements. Integer
  changes of primitive basis produce equivalent energy wells. Plastic
  rearrangement follows from transitions between wells under quasistatic
  loading and relaxation. The element energy is the energy density times its
  reference area. Stress and stiffness follow by differentiation through the
  active reduction and reference maps.
  [Chapter 2 §§3–5, pp. 43–61](PhD_Thesis_Lundheim.pdf#page=54) ·
  [Appendix B.4–B.6, pp. 179–187](PhD_Thesis_Lundheim.pdf#page=190)
- Distinguish a basis-reduction boundary, loss of strong ellipticity, and a
  relaxed collective plastic event. Loss of strong ellipticity is the
  vanishing of the minimum acoustic-tensor eigenvalue for some propagation
  direction. A plotted boundary in laboratory stress coordinates depends on
  elastic rotation. When diagnosing poor conditioning, distinguish reference
  and current element shape, deformation-gradient conditioning, constitutive
  curvature, and the nodal tangent with translations removed.
  [Chapter 4 §4.3, pp. 118–124](PhD_Thesis_Lundheim.pdf#page=129) ·
  [Appendix A.2, pp. 165–174](PhD_Thesis_Lundheim.pdf#page=176) ·
  [Appendix B.6.5, p. 187](PhD_Thesis_Lundheim.pdf#page=198)

## Mesh reconnection and plastic history

- Fixed connectivity can obstruct slip and create topological locking.
  Reconnection updates neighbouring element groups at fixed nodal positions.
  The local edge-flip test uses the mesh metric and a Voronoi-neighbour
  condition. A canonical reference triangle avoids degenerate reference
  Jacobians. Compatible affine flips preserve the represented lattice and
  mechanical response. Non-affine flips can change element energy and stress.
  The thesis tests acceptance on relaxed energy after a reconnection trial.
  [Chapter 3 §§2–4, pp. 67–85](PhD_Thesis_Lundheim.pdf#page=78) ·
  [Algorithm 3, p. 82](PhD_Thesis_Lundheim.pdf#page=93)
- Square-lattice plastic reduction factors $F=F_eF_p$ with
  $F_e=FM_p$, $F_p=M_p^{-1}$, and $M_p$ selected from elementary
  integer shears. The shortest admissible path selects a representative in the
  fundamental elastic well, with well boundaries requiring special care.
  Lagrange reduction serves lattice equivalence and energy evaluation.
  Plastic reduction additionally selects the integer transformation that
  represents slip.
  [Chapter 4 §§1–2, pp. 92–107](PhD_Thesis_Lundheim.pdf#page=103)
- Across an edge flip, the thesis assigns a donor to each new element and
  transports its accumulated plastic factor $T_p$. Rule I inherits
  $T_p$ and recomputes the elastic factor from the new element, giving
  $T^+=F_e^+T_p^-$. With compatible elastic representatives this preserves
  $T$ under affine flips and permits a change under non-affine flips.
  The inheritance rule and the canonical reference orientation are distinct
  choices. Verify the implementation and field names at the recorded
  simulator revision before analysing a run.
  [Chapter 4 §3, pp. 108–113](PhD_Thesis_Lundheim.pdf#page=119) ·
  [Appendix B.10, pp. 190–192](PhD_Thesis_Lundheim.pdf#page=201)

## Avalanche statistics and scope

- The avalanche observable is the stress-corrected energy drop
  $\Delta E_S$. The thesis uses an energy increment $\Delta E_R$ and
  reverse-loading displacement closure to distinguish irreversible events
  from ordinary relaxation. Statistical fits use positive $\Delta E_S$ for
  the selected irreversible events, maximum-likelihood estimation with a
  cutoff, and a Kolmogorov–Smirnov procedure to select the lower fit bound.
  [Chapter 5 §§1–2, pp. 127–149](PhD_Thesis_Lundheim.pdf#page=138)
- Reconnection alleviates artificial energy growth and permits continued
  intermittent plastic flow and grain-like organization under large shear.
  L-BFGS, nonlinear conjugate gradient, and FIRE differ in relaxation paths and
  cost. Heavy-tailed avalanche statistics remain qualitatively similar in the
  sizes and strain intervals studied. Fit cutoffs and goodness-of-fit limit
  claims about universal exponents. The grain-like organization was observed
  in idealized two-dimensional periodic simulations.
  [Chapter 3 §4.5, pp. 85–89](PhD_Thesis_Lundheim.pdf#page=96) ·
  [Chapter 5 §§3–4, pp. 149–159](PhD_Thesis_Lundheim.pdf#page=160) ·
  [General conclusions, pp. 160–163](PhD_Thesis_Lundheim.pdf#page=171)

## MTS2D output conventions

In full-field VTUs, `G11`, `G12`, and `G22` are the mesh-edge Gram matrix
$G^{(e)}$. For the crystal lattice angle, construct $G=H^\mathsf TCH$ from
the saved strain $C$ and the constitutive reference in the recorded run
configuration. The square reference uses $H=I$ and the triangular reference
uses the unit-area map in Appendix B.3. Reduce $G$ to $G_R$ using the saved
Lagrange change of basis when available. The reduced lattice angle is
$\arccos(|(G_R)_{12}|/\sqrt{(G_R)_{11}(G_R)_{22}})$.
Use a spatial basis or a separately defined elastic rotation for orientation
relative to the laboratory axes.

[MTS2D VTU writer](../../MTS2D/src/Data/data_vtu.cpp) ·
[MTS2D lattice reduction](../../MTS2D/src/Mesh/tElement.cpp)
