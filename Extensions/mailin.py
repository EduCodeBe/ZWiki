"""
zwiki mailin - post an incoming email message to a wiki

This is an external method for receiving mail from a mailer alias,
procmail recipe or script via something like:
| curl -n -F 'msg=<-' http://mysite/mywikifolder/mailin
and posting it to a suitable zwiki page.  It expects at least one
argument, an RFC822 message.

Here are the delivery rules:
XXX see the latest CHANGES.txt for some updates to these.

- if the message appears to be a zwiki mailout or from an auto-responder
  or junk, silently discard it

- if called in a page context (http://site/wikipage/mailin), always use
  that page. Otherwise,

- (DISABLED: if a recipient of the form
  ".*(wiki|mailin|tracker|bugs|issues)@virtualhost" (MAILINADDREXP) is
  found, where virtualhost matches an existing VirtualHostMonster entry,
  then in the corresponding folder, otherwise,)

- in the current folder (which must contain at least one zwiki page),

- unless called with subscribersonly=0 or the folder's mailin_policy
  property is 'open' (old posting_policy property also supported), check
  that the sender (From or Sender address) is subscribed somewhere in the
  wiki; or, is listed in the mail_accept_nonmembers property. If not,
  bounce the message.

- decide which of the recipients is us, as follows:

   1. if there's only one, use that one
   2. or the first one whose address matches the folder's mail_from property
   3. or the first one whose address matches MAILINADDREXP
   4. or the first one

  NB cases 3 and 4 may sometimes lead it to guess the wrong recipient and
  potentially deliver to the wrong page. Perhaps we can get this from the
  mail servers and pass it as an argument.

- if called with trackerissue=1 or the recipient matches
  ".*(tracker|bugs|issues)@" (TRACKERADDREXP), create a tracker issue page.

- Otherwise, look for

   1. a non-empty page name in the recipient real name (PAGEINREALNAMEEXP)
   2. or the first WikiName or [bracketed name] in the subject
   3. or the folder's default_page property (possibly acquired)
   4. or the defaultpage argument we were called with (XXX remove ?)
   5. or the DEFAULTPAGE defined below
   6. or the first zwiki page in the folder (as returned by objectValues)

- and add a comment to that page, creating it if necessary.

Note page creation and comments will trigger subscriber mail-outs as usual.

Tests for this code are in ZWiki/tests/testMailin.py.

todo:
size limits
friendly bounce messages
integrate pb's http posting, attachment handling ?
use python 2.2 email package ?

"""

from types import *
import re, string 
import rfc822, mimetools, multifile, StringIO
from Products.ZWiki.Regexps import wikiname1,wikiname2,bracketedexpr
from Products.ZWiki.Utils import BLATHER

DEFAULTPAGE = 'FrontPage'
#PAGEINSUBJECTEXP = r'(%s|%s)' % (wikiname1,wikiname2)
#PAGEINSUBJECTEXP = r'(%s|%s|%s)' % (wikiname1,wikiname2,bracketedexpr)
PAGEINSUBJECTEXP = bracketedexpr
MAILINADDREXP = r'(wiki|mailin|tracker|bugs|issues)@'
TRACKERADDREXP = r'(tracker|bugs|issues)@'
# extract a page name from the recipient real name
# because different things show up here - 'name', mail address, etc. -
# we recognize only page names beginning and ending with a word character
# and not containing @
PAGEINREALNAMEEXP = r'(?=^[^@]*$).*?(?P<page>\w.*\w)' 


def isJunk(msgtext):
    """
    Return true if this message should be silently ignored.

    Ideally, this should block mail loops, auto-responders and spam,
    but allow mailing list messages and mailouts from other zwikis.
    Currently it flags everything that looks like it came from a bot.
    Actually I don't think works as a way to filter autoresponders.
    We should make sure to include mailing headers in our mailouts
    and then most autoresponders should ignore us.

    qmail-autoresponder's bot-filtering procedure is reportedly good - see
    http://untroubled.org/qmail-autoresponder/procedure.txt .
    TMDA and spamassassin are two good spam filters - see
    http://software.libertine.org/tmda ,
    http://spamassassin.taint.org .
    """
    # a zwiki mailout
    if re.search(r'(?mi)^X-Zwiki-Version:',msgtext):
        return 1
    # the most common auto-response subject
    if re.search(r'(?mi)^Subject:.*out of office',msgtext):
        return 1
    # XXX don't need this ?
    # mailing list or low precedence mail
    # should allow these, but need to pass through the list loop
    # headers to avoid mail loop between mutually-subscribed zwiki and
    # mail list
    #if re.search(r'(?mi)^(List-ID|(X-)?Mailing-List|X-ML-Name):',msgtext):
    #    return 1
    #if re.search(
    #    r'(?mi)^List-(Help|Unsubscribe|Subscribe|Post|Owner|Archive):',msgtext):
    #    return 1
    #if re.search(r'(?mi)^Precedence:\s*(junk|bulk|list)\s*$',msgtext):
    #    return 1
    return 0


class MailIn:
    """
    An incoming wikimail message.

    Responsibilities:
    - parse the message and expose the parts of interest.
    - figure out how to deliver itself as per the rules above.

    Uses the rfc822 package so as to work with python 1.5.
    """
    folder = None
    recipient = None
    destpage = None
    destpagename = None
    workingpage = None
    trackerissue = 0
    creating = 0
    error = None
    
    def __init__(self,
                 context,
                 message,
                 defaultpage=DEFAULTPAGE,
                 subscribersonly=1,
                 trackerissue=0,
                 checkrecipient=1,
                 checksubject=1,
                 ):
        BLATHER('mailin.py: processing incoming message:\n%s' % message)

        # parse the message
        self.context = context
        self.raw = message
        self.stream = StringIO.StringIO(self.raw)
        self.msg = mimetools.Message(self.stream)
        self.date = self.msg['Date']
        self.subject = (self.msg.has_key('Subject') and self.msg['Subject']) or ''
        self.realSubject = re.sub(r'.*?\[.*?\](.*)',r'\1',self.subject)
	# XXX x-beenthere is mailman-specific - need to support ezmlm & others here
        #self.xbeenthere = (self.msg.get('X-BeenThere') or
	#                   re.search(r'[^\s<]+@[^\s>]+',self.msg.get('Delivered-To')).group())
	# Type Error
	# made ezmlm include beenthere
        self.xbeenthere = self.msg.get('X-BeenThere')
        self.messageid = self.msg.get('Message-id')
        self.inreplyto = self.msg.get('In-reply-to')
        self.From = self.msg.get('From')
        self.FromRealName = rfc822.parseaddr(self.From)[0]
        self.FromEmail    = rfc822.parseaddr(self.From)[1]
        self.FromUserName = (self.FromRealName or
                             re.sub(r'@.*$',r'',self.FromEmail))
        self.sender = self.msg.get('Sender')
        self.senderEmail = (self.sender and
                            rfc822.parseaddr(self.sender)[1]) or None
        self.recipients = []
        for hdr in ['To','Cc','Bcc']:
            if self.msg.has_key(hdr):
                self.recipients.extend(self.msg.getaddrlist(hdr))
        if self.isMime():
            self.body = self.firstMimePartMatching('text/plain')
        else:
            self.body = self.msg.fp.read()
        # strip trailing newlines that seem to get added in transit
        self.body = re.sub(r'(?s)\n+$',r'\n',self.body)
        # strip TBC (typical bloody citations)
        #self.body = re.sub(
        #    r'(?si)----- ?message d\'origine.*',r'',self.body)
        #self.body = re.sub(
        #    r'(?si)----- ?original message.*',r'',self.body)

        # figure out where it should go
        if self.context.meta_type == 'ZWiki Page':
            self.folder = self.context.aq_parent
            self.destpage = self.workingpage = self.context
            self.destpagename = self.destpage.title_or_id()
            self.creating = 0
        # or at least the folder and a working page
        else:
            self.folder = self.context
            # find default and working pages..
            # NB the default page may have a freeform name
            defaultpagename = getattr(self.folder,'default_page',defaultpage)
            self.workingpage = getattr(self.folder,defaultpagename,None)
            if not self.workingpage:
                allpages = self.folder.objectValues(spec='ZWiki Page')
                if len(allpages):
                    firstpage = allpages[0]
                    self.workingpage = \
                        firstpage.pageWithName(defaultpagename) or firstpage
                    defaultpagename = self.workingpage.title_or_id()
                else:
                    BLATHER('mailin.py: could not find a working page')
                    self.error = '\nSorry, I could not find an existing wiki page to work with.\n\n\n'
                    return

        # Check if the sender (From or Sender address) is allowed to post
        # there.
        # - if open posting is allowed, we'll accept anybody.
        # - otherwise they must be a subscriber somewhere in the wiki.
        # - or, if the wiki is gatewayed with a mailing list we want to
        # accept list subscribers. Since we're not fully integrated with
        # mailman (and to support other list software) I think the
        # simplest thing is a mailman-style option for extra acceptable
        # posters, containing eg the list's Sender address (-bounces).
        postingpolicy = getattr(self.folder,'mailin_policy',
                                getattr(self.folder,'posting_policy',None))
        accept = getattr(self.folder,'mail_accept_nonmembers',[])
        if not subscribersonly or postingpolicy == 'open':
            pass
        # XXX poor caching
        elif (self.workingpage.allSubscriptionsFor(self.FromEmail) or
              self.workingpage.allSubscriptionsFor(self.senderEmail)):
            pass
        elif (self.FromEmail in accept or self.senderEmail in accept):
            pass
        else:
            self.error = '\nSorry, you must be a subscriber to send mail to this wiki.\n\n\n'
            BLATHER('mailin.py: bounced mail from non-subscriber',
                 self.FromEmail)
            return

        # decide which of the recipients is us
        if len(self.recipients) == 1:
            self.recipient = self.recipients[0]
        elif getattr(self.folder,'mail_from',None):
            folder_mail_from = getattr(self.folder,'mail_from')
            for r in self.recipients:
                if r[1] == folder_mail_from:
                    self.recipient = r
                    break
        if not self.recipient:
            for r in self.recipients:
                if re.search(MAILINADDREXP,r[1]):
                    self.recipient = r
                    break
        if not self.recipient:
            self.recipient = self.recipients[0]

        # decide if this is a tracker issue
        if (trackerissue or re.search(TRACKERADDREXP,self.recipient[1])):
            self.trackerissue = 1
            return

        # finalize our choice of destination page
        if not self.destpage:
            # look for a page name in recipient real name
            if checkrecipient:
                m = re.search(PAGEINREALNAMEEXP,self.recipient[0])
                if m:
                    self.destpagename = m.group('page')
            # or in the subject
            if (not self.destpagename) and checksubject:
                matches = re.findall(PAGEINSUBJECTEXP,self.subject)
                if matches:
                    self.destpagename = matches[-1] # use rightmost
            # (and strip enclosing []'s if needed)
            if self.destpagename:
                self.destpagename = re.sub(bracketedexpr, r'\1',
                                           self.destpagename)
            # or use the default page name
            if not self.destpagename:
                self.destpagename = defaultpagename
                    
            # destination page identified!
            # use it if it exists, otherwise we'll create
            if self.destpagename and \
               self.workingpage.pageWithFuzzyName(self.destpagename,
                                                  ignore_case=1):
                self.workingpage = self.destpage = \
                     self.workingpage.pageWithFuzzyName(self.destpagename,
                                                        ignore_case=1)
                self.creating = 0
            else:
                self.creating = 1

    def isMime(self):
        return re.search(r'Content-Type:.*multipart',self.raw)

    # based on python manual example
    def firstMimePartMatching(self,mimetype):
        msgtype = self.msg.gettype()
        params = self.msg.getplist()
        data = StringIO.StringIO()
        if msgtype[:10] == "multipart/":
            file = multifile.MultiFile(self.stream)
            file.push(self.msg.getparam("boundary"))
            while file.next():
                submsg = mimetools.Message(file)
                try:
                    data = StringIO.StringIO()
                    mimetools.decode(file, data, submsg.getencoding())
                except ValueError:
                    continue
                if submsg.gettype() == mimetype:
                    break
            file.pop()
        return data.getvalue()
    
#    def findDestinationVirtualHost(self, msg):
#        """
#        If we have one of the special recipients listed above along with a
#        matching virtual host, return the virtual host's folder; otherwise None.
#        Also save the username part of the matched recipient for later.
#        """
#        folder = None
#        vurt = getattr(self,'vurt',None)
#        if vurt and msg.wikiRecipients:
#            for address, realname in msg.wikiRecipients:
#                alias, host = address
#                # based on SiteAccess
#                # extract this -->
#                dict = getattr(vurt, 'domain_path', None)
#                if dict:
#                    real_host=None
#                    l_host = string.lower(host)
#                    if vurt.domain_path.has_key(l_host):
#                        real_host=l_host
#                    else:
#                        for hostname in vurt.domain_path.keys():
#                            if hostname[:2] == '*.':
#                                if string.find(l_host, hostname[2:]) != -1:
#                                    real_host = hostname
#                    # <--
#                    if real_host:
#                        path = vurt.domain_path[real_host]
#                        folder = self.getPhysicalRoot().unrestrictedTraverse(path)
#                        msg.aliasUsed = alias
#                        break
#        return folder


def mailin(self,
           msg,
           pagenameexp=None, #XXX temporary backwards compatibility
           defaultpage=DEFAULTPAGE,
           separator=None, #XXX temporary backwards compatibility
           checkrecipient=0,
           checksubject=1,
           trackerissue=0,
           subscribersonly=1,
           ):
    context = self
    # these come over the web as strings
    checkrecipient  = int(checkrecipient or 0)
    checksubject    = int(checksubject or 0)
    trackerissue    = int(trackerissue or 0)
    subscribersonly = int(subscribersonly or 0)

    # silently discard any junk
    if isJunk(msg): return

    # parse and figure out how to deliver the message
    m = MailIn(context,
               msg,
               defaultpage=defaultpage,
               subscribersonly=subscribersonly,
               trackerissue=trackerissue,
               checkrecipient=checkrecipient,
               checksubject=checksubject,
               )
    if m.error:
        return m.error

    # stash the sender's username in REQUEST so zwiki can use it to set
    # last_editor
    self.REQUEST.set('MAILIN_USERNAME', m.FromUserName)

    # a new tracker issue ?
    if m.trackerissue:
        subject = m.subject or 'no subject'
        self.REQUEST.set('newtitle', subject)
        # XXX italicize quoted text in replies (as in comment().. but DRY)
        body = re.sub(r'(?m)^>(.*)',r'<br>><i>\1</i>',m.body)
        self.REQUEST.set('newtext', body)
        self.REQUEST.set('submitted', 1)
        m.folder.IssueTracker(REQUEST=self.REQUEST)
        #BLATHER('mailin.py: created issue '+subject)
        return 

    # a new page ?
    subject = m.realSubject
    if m.creating:
        subject = '(new) '+subject
        # XXX may need to pass REQUEST for authentication ?
        # but "REQUEST has no URL2" and create fails. Leave out for now.
        m.workingpage.create(m.destpagename,text='')
        m.destpage = m.workingpage.pageWithName(m.destpagename)
        #BLATHER('mailin.py: created '+m.destpagename)

    # add comment
    # use the time of sending, or the time of posting to the wiki - see
    # how the latter works out

    # mailing list support: when a list and wiki are mutually subscribed,
    # and a mail comes in from the list, we want to forward it out to all
    # subscribers except the list, which has done it's own delivery. So,
    # tell zwiki to exclude the X-BeenThere address from mailout. We could
    # just rely on the list's duplicate filtering if message-id was
    # preserved.
    m.destpage.comment(text=m.body,
                       username=m.FromUserName,
                       REQUEST=self.REQUEST,
                       subject_heading=subject,
                       exclude_address=m.xbeenthere,
                       message_id=m.messageid,
                       in_reply_to=m.inreplyto
                       )
    #BLATHER('mailin.py: commented on '+m.destpagename)
