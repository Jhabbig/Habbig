(function() {
  'use strict';

  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  // Intersection Observer for reveal animations
  var revealObserver = new IntersectionObserver(function(entries) {
    entries.forEach(function(entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add('is-visible');
        revealObserver.unobserve(entry.target);
      }
    });
  }, {
    threshold: 0.12,
    rootMargin: '0px 0px -48px 0px'
  });

  var selectors = '.reveal,.reveal-fade,.reveal-scale,.reveal-left,.reveal-right,.reveal-line,.reveal-number';
  document.querySelectorAll(selectors).forEach(function(el) {
    revealObserver.observe(el);
  });

  // Stagger children
  document.querySelectorAll('.stagger').forEach(function(container) {
    Array.from(container.children).forEach(function(child) {
      if (!child.classList.contains('reveal') &&
          !child.classList.contains('reveal-scale') &&
          !child.classList.contains('reveal-fade')) {
        child.classList.add('reveal');
        revealObserver.observe(child);
      }
    });
  });

  // Number counter
  function animateCounter(el) {
    var target = parseFloat(el.dataset.target || el.textContent);
    var isFloat = el.dataset.target && el.dataset.target.indexOf('.') !== -1;
    var duration = 1200;
    var start = performance.now();

    function update(now) {
      var elapsed = now - start;
      var progress = Math.min(elapsed / duration, 1);
      var eased = 1 - Math.pow(1 - progress, 3);
      var current = target * eased;
      el.textContent = isFloat ? current.toFixed(2) : Math.floor(current).toLocaleString();
      if (progress < 1) requestAnimationFrame(update);
    }
    requestAnimationFrame(update);
  }

  var numberObserver = new IntersectionObserver(function(entries) {
    entries.forEach(function(entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add('is-visible');
        animateCounter(entry.target);
        numberObserver.unobserve(entry.target);
      }
    });
  }, { threshold: 0.5 });

  document.querySelectorAll('.reveal-number').forEach(function(el) {
    el.dataset.target = el.textContent;
    el.textContent = '0';
    numberObserver.observe(el);
  });

  // Parallax
  var parallaxEls = document.querySelectorAll('.parallax-slow');
  if (parallaxEls.length > 0) {
    var ticking = false;
    var isMobile = window.innerWidth < 768;

    function updateParallax() {
      var scrollY = window.scrollY;
      parallaxEls.forEach(function(el) {
        var rect = el.getBoundingClientRect();
        var speed = parseFloat(el.dataset.parallaxSpeed || 0.3);
        if (isMobile) speed *= 0.5;
        var offset = (rect.top + scrollY) - window.innerHeight / 2;
        el.style.transform = 'translateY(' + (offset * speed * -1) + 'px)';
      });
      ticking = false;
    }

    window.addEventListener('scroll', function() {
      if (!ticking) {
        requestAnimationFrame(updateParallax);
        ticking = true;
      }
    }, { passive: true });
  }
})();
