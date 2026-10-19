# The study steps, and where they come from

The engine's checklist follows the steps that the simulation literature agrees on. This page paraphrases
them, so you can explain a step to the person or decide how much of it a study needs.

1. **Formulate the problem.** The decision, who makes it, the options and the measures of performance
   (Law, ch. 1; Banks et al., ch. 1).
2. **Conceptual model.** What is in scope and what is not, at which level of detail, and the assumptions
   and simplifications, written down and agreed before coding (Robinson; Law's "assumptions document").
3. **Data.** Collect what exists, fit distributions where it is variable, and record the source and age
   of every input; an expert estimate is an input too, recorded as such (Law, ch. 6).
4. **Build and verify.** Does the model do what was intended? Trace a few entities, check flow balance
   and utilisation against hand calculations, run with the same seed twice (Sargent: verification).
5. **Validate.** Does it match reality closely enough for the decision? Compare with data, with a known
   answer for a simplified case (a queue with a textbook result), or with an expert's judgement, and say
   which (Sargent: operational validity; face validity is the weakest).
6. **Experiment.** Warm-up when the system starts empty, enough replications for a confidence interval
   that can tell the options apart, the same random numbers across scenarios (Law, ch. 9-12).
7. **Report.** The answer to the question, how sure, the assumptions and their sources, the validation
   done, what was left out (STRESS for discrete-event, agent-based and system dynamics studies; ODD for
   agent-based models).

## Sources

- Law, A. M. *Simulation Modeling and Analysis*, 5th ed. McGraw-Hill, 2015.
- Banks, J., Carson, J. S., Nelson, B. L., Nicol, D. M. *Discrete-Event System Simulation*, 5th ed. Pearson, 2010.
- Robinson, S. *Simulation: The Practice of Model Development and Use*, 2nd ed. Palgrave, 2014.
- Sargent, R. G. "Verification and validation of simulation models." *Journal of Simulation* 7(1), 2013.
- Monks, T. et al. "Strengthening the reporting of empirical simulation studies: Introducing the STRESS
  guidelines." *Journal of Simulation* 13(1), 2019.
- Grimm, V. et al. "The ODD protocol for describing agent-based and other simulation models: A second
  update." *JASSS* 23(2), 2020.
