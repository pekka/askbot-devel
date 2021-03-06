# encoding:utf-8
import datetime
import logging
from urllib import unquote
from django.conf import settings
from django.shortcuts import render_to_response, get_object_or_404
from django.http import HttpResponseRedirect, HttpResponse, HttpResponseForbidden, Http404
from django.core.paginator import Paginator, EmptyPage, InvalidPage
from django.template import RequestContext
from django.utils.html import *
from django.utils import simplejson
from django.db.models import Q
from django.utils.translation import ugettext as _
from django.template.defaultfilters import slugify
from django.core.urlresolvers import reverse
from django.views.decorators.cache import cache_page

from forum.utils.html import sanitize_html
from markdown2 import Markdown
#from lxml.html.diff import htmldiff
from forum.utils.diff import textDiff as htmldiff
from forum.forms import *
from forum.models import *
from forum.auth import *
from forum.const import *
from forum import const
from forum import auth
from forum.utils.forms import get_next_url
from forum.search.state_manager import SearchState

# used in index page
#refactor - move these numbers somewhere?
INDEX_PAGE_SIZE = 30
INDEX_AWARD_SIZE = 15
INDEX_TAGS_SIZE = 25
# used in tags list
DEFAULT_PAGE_SIZE = 60
# used in questions
# used in answers
ANSWERS_PAGE_SIZE = 10

markdowner = Markdown(html4tags=True)

#system to display main content
def _get_tags_cache_json():#service routine used by views requiring tag list in the javascript space
    """returns list of all tags in json format
    no caching yet, actually
    """
    tags = Tag.objects.filter(deleted=False).all()
    tags_list = []
    for tag in tags:
        dic = {'n': tag.name, 'c': tag.used_count}
        tags_list.append(dic)
    tags = simplejson.dumps(tags_list)
    return tags

#refactor? - we have these
#views that generate a listing of questions in one way or another:
#index, unanswered, questions, search, tag
#should we dry them up?
#related topics - information drill-down, search refinement

def index(request):#generates front page - shows listing of questions sorted in various ways
    """index view mapped to the root url of the Q&A site
    """
    return HttpResponseRedirect(reverse('questions'))

#todo: eliminate this from urls
def unanswered(request):#generates listing of unanswered questions
    return questions(request, unanswered=True)

def questions(request):#a view generating listing of questions, used by 'unanswered' too
    """
    List of Questions, Tagged questions, and Unanswered questions.
    """

    #don't allow to post to this view
    if request.method == 'POST':
        raise Http404

    #todo: redo SearchState to accept input from
    #view_log, session and request parameters
    search_state = request.session.get('search_state', SearchState())

    view_log = request.session['view_log']

    if view_log.get_previous(1) != 'questions':
        if view_log.get_previous(2) != 'questions':
            #print 'user stepped too far, resetting search state'
            search_state.reset()

    if request.user.is_authenticated():
        search_state.set_logged_in()

    form = AdvancedSearchForm(request.GET)
    #todo: form is used only for validation...
    if form.is_valid():
        search_state.update_from_user_input(
                                                form.cleaned_data, 
                                                request.GET, 
                                            )
        #todo: better put these in separately then analyze
        #what neesd to be done, otherwise there are two routines
        #that take request.GET I don't like this use of parameters
        #another weakness is that order of routine calls matters here
        search_state.relax_stickiness( request.GET, view_log )

        request.session['search_state'] = search_state
        request.session.modified = True

    #force reset for debugging
    #search_state.reset()
    #request.session.modified = True

    #have this call implemented for sphinx, mysql and pgsql
    (qs, meta_data) = Question.objects.run_advanced_search(
                            request_user = request.user,
                            scope_selector = search_state.scope,#unanswered/all/favorite (for logged in)
                            search_query = search_state.query,
                            tag_selector = search_state.tags,
                            author_selector = search_state.author,
                            sort_method = search_state.sort
                        )

    objects_list = Paginator(qs, search_state.page_size)
    questions = objects_list.page(search_state.page)

    #todo maybe do this search on query the set instead
    related_tags = Tag.objects.get_tags_by_questions(questions.object_list)

    tags_autocomplete = _get_tags_cache_json()

    contributors = Question.objects.get_question_and_answer_contributors(questions.object_list)

    #todo: organize variables by type
    return render_to_response('questions.html', {
        'questions' : questions,
        'contributors' : contributors,
        'author_name' : meta_data.get('author_name',None),
        'tab_id' : search_state.sort,
        'questions_count' : objects_list.count,
        'tags' : related_tags,
        'query': search_state.query,
        'search_tags' : search_state.tags,
        'tags_autocomplete' : tags_autocomplete,
        'is_unanswered' : False,#remove this from template
        'interesting_tag_names': meta_data.get('interesting_tag_names',None),
        'ignored_tag_names': meta_data.get('ignored_tag_names',None), 
        'sort': search_state.sort,
        'scope': search_state.scope,
        'context' : {
            'is_paginated' : True,
            'pages': objects_list.num_pages,
            'page': search_state.page,
            'has_previous': questions.has_previous(),
            'has_next': questions.has_next(),
            'previous': questions.previous_page_number(),
            'next': questions.next_page_number(),
            'base_url' : request.path + '?sort=%s&' % search_state.sort,#todo in T sort=>sort_method
            'page_size' : search_state.page_size,#todo in T pagesize -> page_size
        }}, context_instance=RequestContext(request))

def search(request): #generates listing of questions matching a search query - including tags and just words
    """redirects to people and tag search pages
    todo: eliminate this altogether and instead make
    search "tab" sensitive automatically - the radio-buttons
    are useless under the search bar
    """
    if request.method == "GET":
        search_type = request.GET.get('t')
        query = request.GET.get('query')
        try:
            page = int(request.GET.get('page', '1'))
        except ValueError:
            page = 1
        if search_type == 'tag':
            return HttpResponseRedirect(reverse('tags') + '?q=%s&page=%s' % (query.strip(), page))
        elif search_type == 'user':
            return HttpResponseRedirect(reverse('users') + '?q=%s&page=%s' % (query.strip(), page))
        else:
            raise Http404
    else:
        raise Http404

def tag(request, tag):#stub generates listing of questions tagged with a single tag
    return questions(request, tagname=tag)

def tags(request):#view showing a listing of available tags - plain list
    stag = ""
    is_paginated = True
    sortby = request.GET.get('sort', 'used')
    try:
        page = int(request.GET.get('page', '1'))
    except ValueError:
        page = 1

    if request.method == "GET":
        stag = request.GET.get("q", "").strip()
        if stag != '':
            objects_list = Paginator(Tag.objects.filter(deleted=False).exclude(used_count=0).extra(where=['name like %s'], params=['%' + stag + '%']), DEFAULT_PAGE_SIZE)
        else:
            if sortby == "name":
                objects_list = Paginator(Tag.objects.all().filter(deleted=False).exclude(used_count=0).order_by("name"), DEFAULT_PAGE_SIZE)
            else:
                objects_list = Paginator(Tag.objects.all().filter(deleted=False).exclude(used_count=0).order_by("-used_count"), DEFAULT_PAGE_SIZE)

    try:
        tags = objects_list.page(page)
    except (EmptyPage, InvalidPage):
        tags = objects_list.page(objects_list.num_pages)

    return render_to_response('tags.html', {
                                            "active_tab": "tags",
                                            "tags" : tags,
                                            "stag" : stag,
                                            "tab_id" : sortby,
                                            "keywords" : stag,
                                            "context" : {
                                                'is_paginated' : is_paginated,
                                                'pages': objects_list.num_pages,
                                                'page': page,
                                                'has_previous': tags.has_previous(),
                                                'has_next': tags.has_next(),
                                                'previous': tags.previous_page_number(),
                                                'next': tags.next_page_number(),
                                                'base_url' : reverse('tags') + '?sort=%s&' % sortby
                                            }
                                }, context_instance=RequestContext(request))

def question(request, id):#refactor - long subroutine. display question body, answers and comments
    """view that displays body of the question and 
    all answers to it
    """
    try:
        page = int(request.GET.get('page', '1'))
    except ValueError:
        page = 1

    view_id = request.GET.get('sort', None)
    view_dic = {"latest":"-added_at", "oldest":"added_at", "votes":"-score" }
    try:
        orderby = view_dic[view_id]
    except KeyError:
        qsm = request.session.get('questions_sort_method',None)
        if qsm in ('mostvoted','latest'):
            logging.debug('loaded from session ' + qsm)
            if qsm == 'mostvoted':
                view_id = 'votes'
                orderby = '-score'
            else:
                view_id = 'latest'
                orderby = '-added_at'
        else:
            view_id = "votes"
            orderby = "-score"

    logging.debug('view_id=' + str(view_id))

    question = get_object_or_404(Question, id=id)
    try:
        pattern = r'/%s%s%d/([\w-]+)' % (settings.FORUM_SCRIPT_ALIAS,_('question/'), question.id)
        path_re = re.compile(pattern)
        logging.debug(pattern)
        logging.debug(request.path)
        m = path_re.match(request.path)
        if m:
            slug = m.group(1)
            logging.debug('have slug %s' % slug)
            assert(slug == slugify(question.title))
        else:
            logging.debug('no match!')
    except:
        return HttpResponseRedirect(question.get_absolute_url())

    if question.deleted and not auth.can_view_deleted_post(request.user, question):
        raise Http404
    answer_form = AnswerForm(question,request.user)
    answers = Answer.objects.get_answers_from_question(question, request.user)
    answers = answers.select_related(depth=1)

    favorited = question.has_favorite_by_user(request.user)
    if request.user.is_authenticated():
        question_vote = question.votes.select_related().filter(user=request.user)
    else:
        question_vote = None #is this correct?
    if question_vote is not None and question_vote.count() > 0:
        question_vote = question_vote[0]

    user_answer_votes = {}
    for answer in answers:
        vote = answer.get_user_vote(request.user)
        if vote is not None and not user_answer_votes.has_key(answer.id):
            vote_value = -1
            if vote.is_upvote():
                vote_value = 1
            user_answer_votes[answer.id] = vote_value

    if answers is not None:
        answers = answers.order_by("-accepted", orderby)

    filtered_answers = []
    for answer in answers:
        if answer.deleted == True:
            if answer.author_id == request.user.id:
                filtered_answers.append(answer)
        else:
            filtered_answers.append(answer)

    objects_list = Paginator(filtered_answers, ANSWERS_PAGE_SIZE)
    page_objects = objects_list.page(page)

    #todo: merge view counts per user and per session
    #1) view count per session
    update_view_count = False
    if 'question_view_times' not in request.session:
        request.session['question_view_times'] = {}

    last_seen = request.session['question_view_times'].get(question.id,None)
    updated_when, updated_who = question.get_last_update_info()

    if updated_who != request.user:
        if last_seen:
            if last_seen < updated_when:
                update_view_count = True 
        else:
            update_view_count = True

    request.session['question_view_times'][question.id] = datetime.datetime.now()

    if update_view_count:
        question.view_count += 1
        question.save()

    #2) question view count per user
    if request.user.is_authenticated():
        try:
            question_view = QuestionView.objects.get(who=request.user, question=question)
        except QuestionView.DoesNotExist:
            question_view = QuestionView(who=request.user, question=question)
        question_view.when = datetime.datetime.now()
        question_view.save()

    return render_to_response('question.html', {
        "question" : question,
        "question_vote" : question_vote,
        "question_comment_count":question.comments.count(),
        "answer" : answer_form,
        "answers" : page_objects.object_list,
        "user_answer_votes": user_answer_votes,
        "tags" : question.tags.all(),
        "tab_id" : view_id,
        "favorited" : favorited,
        "similar_questions" : Question.objects.get_similar_questions(question),
        "context" : {
            'is_paginated' : True,
            'pages': objects_list.num_pages,
            'page': page,
            'has_previous': page_objects.has_previous(),
            'has_next': page_objects.has_next(),
            'previous': page_objects.previous_page_number(),
            'next': page_objects.next_page_number(),
            'base_url' : request.path + '?sort=%s&' % view_id,
            'extend_url' : "#sort-top"
        }
        }, context_instance=RequestContext(request))

QUESTION_REVISION_TEMPLATE = ('<h1>%(title)s</h1>\n'
                              '<div class="text">%(html)s</div>\n'
                              '<div class="tags">%(tags)s</div>')
def question_revisions(request, id):
    post = get_object_or_404(Question, id=id)
    revisions = list(post.revisions.all())
    revisions.reverse()
    for i, revision in enumerate(revisions):
        revision.html = QUESTION_REVISION_TEMPLATE % {
            'title': revision.title,
            'html': sanitize_html(markdowner.convert(revision.text)),
            'tags': ' '.join(['<a class="post-tag">%s</a>' % tag
                              for tag in revision.tagnames.split(' ')]),
        }
        if i > 0:
            revisions[i].diff = htmldiff(revisions[i-1].html, revision.html)
        else:
            revisions[i].diff = QUESTION_REVISION_TEMPLATE % {
                'title': revisions[0].title,
                'html': sanitize_html(markdowner.convert(revisions[0].text)),
                'tags': ' '.join(['<a class="post-tag">%s</a>' % tag
                                 for tag in revisions[0].tagnames.split(' ')]),
            }
            revisions[i].summary = _('initial version') 
    return render_to_response('revisions_question.html', {
                              'post': post,
                              'revisions': revisions,
                              }, context_instance=RequestContext(request))

ANSWER_REVISION_TEMPLATE = ('<div class="text">%(html)s</div>')
def answer_revisions(request, id):
    post = get_object_or_404(Answer, id=id)
    revisions = list(post.revisions.all())
    revisions.reverse()
    for i, revision in enumerate(revisions):
        revision.html = ANSWER_REVISION_TEMPLATE % {
            'html': sanitize_html(markdowner.convert(revision.text))
        }
        if i > 0:
            revisions[i].diff = htmldiff(revisions[i-1].html, revision.html)
        else:
            revisions[i].diff = revisions[i].text
            revisions[i].summary = _('initial version')
    return render_to_response('revisions_answer.html', {
                              'post': post,
                              'revisions': revisions,
                              }, context_instance=RequestContext(request))

